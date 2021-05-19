import os
from os import PathLike
from typing import Union, Dict, Optional

import torch
from torch.testing import assert_allclose

from allennlp.common.testing import AllenNlpTestCase, run_distributed_test, requires_multi_gpu
from allennlp.nn.util import _MODULE_SHARDED_FLAG, load_state_dict_distributed
from allennlp.nn.parallel import FairScaleFsdpWrapper


class EncoderDecoderModel(torch.nn.Module):
    """
    Simple model to use for testing. We use an encoder-decoder architecture with tied
    embeddings to make sure we cover enough edge cases.
    """

    def __init__(self, fsdp_wrapper: FairScaleFsdpWrapper) -> None:
        super().__init__()
        self.embedding = torch.nn.Embedding(12, 4)
        self.emb_proj = fsdp_wrapper.wrap_module(torch.nn.Linear(4, 4))
        self.encoder = fsdp_wrapper.wrap_module(Encoder())
        self.decoder = Decoder(self.embedding, fsdp_wrapper)
        # Add a buffer to make sure these are handled correctly. We don't actually
        # do anything with this though.
        self.register_buffer("buffer", torch.randn(4, 4))

    def tie_weights(self):
        """
        Should be called after loading state dict to make sure embedding weigths are tied.
        """
        self.decoder.linear.weight = self.embedding.weight

    def forward(self, x):
        x = self.embedding(x)
        x = self.emb_proj(x)
        x = self.encoder(x)
        x = self.decoder(x)
        return x


class Encoder(torch.nn.Module):
    def __init__(self) -> None:
        super().__init__()
        self.ff1 = FeedForward()
        self.ff2 = FeedForward()
        # Add a buffer to make sure these are handled correctly. We don't actually
        # do anything with this though.
        self.register_buffer("buffer", torch.randn(4, 4))

    def forward(self, x):
        return self.ff2(self.ff1(x))


class Decoder(torch.nn.Module):
    def __init__(self, embedding: torch.nn.Embedding, fsdp_wrapper: FairScaleFsdpWrapper) -> None:
        super().__init__()
        self.ff = fsdp_wrapper.wrap_module(FeedForward())
        # Don't want to wrap this linear layer since we are tying the weights to the embedding.
        self.linear = torch.nn.Linear(4, 12, bias=False)
        self.linear.weight = embedding.weight
        # Add a buffer to make sure these are handled correctly. We don't actually
        # do anything with this though.
        self.register_buffer("buffer", torch.randn(4, 4))

    def forward(self, x):
        return self.linear(self.ff(x))


class FeedForward(torch.nn.Module):
    def __init__(self) -> None:
        super().__init__()
        self.linear = torch.nn.Linear(4, 4)
        self.activation = torch.nn.ReLU()

    def forward(self, x):
        return self.activation(self.linear(x))


def _dist_load(global_rank: int, world_size: int, gpu_id: int, test_dir: Union[str, PathLike]):
    fsdp_wrapper = FairScaleFsdpWrapper(
        local_rank=global_rank,
        world_size=world_size,
        cuda_device=gpu_id,
        auto_wrap_policy_kwargs={"min_num_params": 1},
    )
    model = EncoderDecoderModel(fsdp_wrapper)

    state_dict: Optional[Dict[str, torch.Tensor]] = None
    if global_rank == 0:
        embedding_weight = torch.randn(12, 4)
        state_dict = {
            "embedding.weight": embedding_weight,
            "emb_proj.weight": torch.randn(4, 4),
            "emb_proj.bias": torch.randn(4),
            "encoder.ff1.linear.weight": torch.randn(4, 4),
            "encoder.ff1.linear.bias": torch.randn(4),
            "encoder.ff2.linear.weight": torch.randn(4, 4),
            "encoder.ff2.linear.bias": torch.randn(4),
            "encoder.buffer": torch.randn(4, 4),
            "decoder.ff.linear.weight": torch.randn(4, 4),
            "decoder.ff.linear.bias": torch.randn(4),
            "decoder.linear.weight": embedding_weight,
            "decoder.buffer": torch.randn(4, 4),
            "buffer": torch.randn(4, 4),
        }
        torch.save(state_dict, os.path.join(test_dir, "state.pt"))

    # Make sure the right modules are sharded.
    assert getattr(model.embedding, _MODULE_SHARDED_FLAG, None) is None
    assert getattr(model.emb_proj, _MODULE_SHARDED_FLAG, None) is True
    assert getattr(model.encoder.ff1.linear, _MODULE_SHARDED_FLAG, None) is True
    assert getattr(model.encoder.ff2.linear, _MODULE_SHARDED_FLAG, None) is True
    assert getattr(model.decoder, _MODULE_SHARDED_FLAG, None) is None
    assert getattr(model.decoder.ff.linear, _MODULE_SHARDED_FLAG, None) is True

    # Now load the state dict... we should be able to do this before wrapping the model itself
    # with the fsdp_wrapper.
    missing_keys, unexpected_keys = load_state_dict_distributed(model, state_dict)
    assert not missing_keys
    assert not unexpected_keys

    # Make sure weights are still tied.
    model.tie_weights()

    # Now wrap outer model.
    model, wrapped_model = fsdp_wrapper.wrap_model(model)

    # Checkpoint each worker's state.
    worker_state = wrapped_model.state_dict()

    # Each tensor should be on the current device.
    for value in worker_state.values():
        assert value.device == torch.device(gpu_id)

    # Save state dict from each worker.
    torch.save(worker_state, os.path.join(test_dir, f"state_worker{gpu_id}.pt"))

    # Now we'll make sure we can successfully do a forward pass, backward pass, and optimizer step.
    optim = torch.optim.Adam(wrapped_model.model.parameters(), lr=0.0001)

    # Do a forward pass.
    x = torch.randint(12, (2, 6)).to(torch.device(gpu_id))
    x = wrapped_model.model(x)
    loss = x.sum()

    # And a backward pass + step.
    loss.backward()
    optim.step()

    # Now save final state.
    torch.save(wrapped_model.state_dict(), os.path.join(test_dir, f"final_state_worker{gpu_id}.pt"))


class TestFairScaleFsdpWrapper(AllenNlpTestCase):
    @requires_multi_gpu
    def test_distributed_loading(self):
        run_distributed_test([0, 1], func=_dist_load, test_dir=self.TEST_DIR)

        # Now make sure the saved state is exactly the same across workers
        state = torch.load(self.TEST_DIR / "state.pt", map_location="cpu")
        state_worker0 = torch.load(self.TEST_DIR / "state_worker0.pt", map_location="cpu")
        state_worker1 = torch.load(self.TEST_DIR / "state_worker1.pt", map_location="cpu")

        assert set(state.keys()) == set(state_worker0.keys())
        assert set(state.keys()) == set(state_worker1.keys())

        for key, tensor in state.items():
            worker0_tensor = state_worker0[key]
            worker1_tensor = state_worker1[key]
            assert_allclose(
                tensor,
                worker0_tensor,
                msg=f"{key} is off in worker 0 state.\nExpected:\n{tensor}\nGot:\n{worker0_tensor}",
            )
            assert_allclose(
                tensor,
                worker1_tensor,
                msg=f"{key} is off in worker 1 state.\nExpected:\n{tensor}\nGot:\n{worker1_tensor}",
            )

        # Gather final state to make sure embeddings stayed tied.
        final_state_worker0 = torch.load(
            self.TEST_DIR / "final_state_worker0.pt", map_location="cpu"
        )
        final_state_worker1 = torch.load(
            self.TEST_DIR / "final_state_worker1.pt", map_location="cpu"
        )
        assert_allclose(
            final_state_worker0["embedding.weight"], final_state_worker0["decoder.linear.weight"]
        )
        assert_allclose(
            final_state_worker0["embedding.weight"], final_state_worker1["embedding.weight"]
        )
