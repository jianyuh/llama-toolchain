# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the terms described in the LICENSE file in
# the root directory of this source tree.

# Copyright (c) Meta Platforms, Inc. and affiliates.
# This software may be used and distributed in accordance with the terms of the Llama 3 Community License Agreement.

import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Generator, List, Optional

import torch
import torch.nn.functional as F
from fairscale.nn.model_parallel.initialize import (
    get_model_parallel_rank,
    initialize_model_parallel,
    model_parallel_is_initialized,
)
from llama_models.llama3_1.api.args import ModelArgs
from llama_models.llama3_1.api.chat_format import ChatFormat, ModelInput
from llama_models.llama3_1.api.datatypes import Message
from llama_models.llama3_1.api.model import Transformer
from llama_models.llama3_1.api.tokenizer import Tokenizer
from termcolor import cprint

from .api.config import CheckpointType, InlineImplConfig
from .api.datatypes import QuantizationType


@dataclass
class TokenResult:
    token: int
    text: str
    logprobs: Optional[List[float]] = None


class Llama:
    @staticmethod
    def build(config: InlineImplConfig):
        """
        Build a Llama instance by initializing and loading a model checkpoint.

        Note:
            This method initializes the distributed process group, sets the device to CUDA,
            and loads the pre-trained model and tokenizer.
        """
        checkpoint = config.checkpoint_config.checkpoint
        if checkpoint.checkpoint_type != CheckpointType.pytorch.value:
            raise NotImplementedError("HuggingFace checkpoints not supported yet")

        if (
            config.quantization
            and config.quantization.type == QuantizationType.fp8.value
        ):
            from .quantization.loader import is_fbgemm_available

            if not is_fbgemm_available():
                raise ImportError("fbgemm-gpu is required for FP8 quantization")

        if not torch.distributed.is_initialized():
            torch.distributed.init_process_group("nccl")

        model_parallel_size = checkpoint.model_parallel_size
        if not model_parallel_is_initialized():
            initialize_model_parallel(model_parallel_size)

        local_rank = int(os.environ.get("LOCAL_RANK", 0))
        torch.cuda.set_device(local_rank)

        # seed must be the same in all processes
        if config.torch_seed is not None:
            torch.manual_seed(config.torch_seed)

        if local_rank > 0:
            sys.stdout = open(os.devnull, "w")

        start_time = time.time()
        ckpt_dir = checkpoint.checkpoint_dir
        checkpoints = sorted(Path(ckpt_dir).glob("*.pth"))
        assert len(checkpoints) > 0, f"no checkpoint files found in {ckpt_dir}"
        assert model_parallel_size == len(
            checkpoints
        ), f"Loading a checkpoint for MP={len(checkpoints)} but world size is {model_parallel_size}"
        ckpt_path = checkpoints[get_model_parallel_rank()]
        state_dict = torch.load(ckpt_path, map_location="cpu", weights_only=True)
        with open(Path(ckpt_dir) / "params.json", "r") as f:
            params = json.loads(f.read())

        # TODO(ashwin): this block is so we can load internal checkpoints without additional
        # fuss. the final code should _not_ have this blurb
        if "model" in params:
            params = params["model"]

        model_args: ModelArgs = ModelArgs(
            max_seq_len=config.max_seq_len,
            max_batch_size=config.max_batch_size,
            **params,
        )
        tokenizer = Tokenizer(model_path=checkpoint.tokenizer_path)

        assert (
            model_args.vocab_size == tokenizer.n_words
        ), f"model_args vocab = {model_args.vocab_size} but tokenizer vocab = {tokenizer.n_words}"

        fp8 = (
            config.quantization
            and config.quantization.type == QuantizationType.fp8.value
        )

        if fp8:
            # load on CPU in bf16 so that fp8 conversion does not find an
            # unexpected (fp32, e.g.) datatype
            torch.set_default_tensor_type(torch.BFloat16Tensor)

        model = Transformer(model_args)

        if fp8:
            # load on CPU first since if we are doing fp8, we probably don't
            # have enough memory on GPU for bf16
            model.load_state_dict(state_dict, strict=False)

        if torch.cuda.is_bf16_supported():
            torch.set_default_tensor_type(torch.cuda.BFloat16Tensor)
        else:
            torch.set_default_tensor_type(torch.cuda.HalfTensor)

        if not fp8:
            model.load_state_dict(state_dict, strict=False)

        if config.quantization:
            from .quantization.loader import convert_to_quantized_model

            model = convert_to_quantized_model(model, config)
        else:
            model = model.to("cuda")

        print(f"Loaded in {time.time() - start_time:.2f} seconds")

        return Llama(model, tokenizer, model_args)

    def __init__(self, model: Transformer, tokenizer: Tokenizer, args: ModelArgs):
        self.args = args
        self.model = model
        self.tokenizer = tokenizer
        self.formatter = ChatFormat(tokenizer)

    @torch.inference_mode()
    def generate(
        self,
        model_input: ModelInput,
        max_gen_len: int,
        temperature: float = 0.6,
        top_p: float = 0.9,
        logprobs: bool = False,
        echo: bool = False,
        include_stop_token: bool = False,
    ) -> Generator:
        params = self.model.params

        # cprint("Input to model -> " + self.tokenizer.decode(model_input.tokens), "red")
        prompt_tokens = [model_input.tokens]

        bsz = 1
        assert bsz <= params.max_batch_size, (bsz, params.max_batch_size)

        min_prompt_len = min(len(t) for t in prompt_tokens)
        max_prompt_len = max(len(t) for t in prompt_tokens)

        if max_prompt_len >= params.max_seq_len:
            cprint(
                f"Out of token budget {max_prompt_len} vs {params.max_seq_len}", "red"
            )
            return

        total_len = min(max_gen_len + max_prompt_len, params.max_seq_len)
        pad_id = self.tokenizer.pad_id
        tokens = torch.full((bsz, total_len), pad_id, dtype=torch.long, device="cuda")
        for k, t in enumerate(prompt_tokens):
            tokens[k, : len(t)] = torch.tensor(t, dtype=torch.long, device="cuda")
        if logprobs:
            token_logprobs = torch.zeros_like(tokens, dtype=torch.float)

        prev_pos = 0
        eos_reached = torch.tensor([False] * bsz, device="cuda")
        input_text_mask = tokens != pad_id
        if min_prompt_len == total_len:
            # TODO(ashwin): unify this branch with the one below and figure out multimodal crap
            logits = self.model.forward(tokens, prev_pos)
            token_logprobs = -F.cross_entropy(
                input=logits.transpose(1, 2),
                target=tokens,
                reduction="none",
                ignore_index=pad_id,
            )

        stop_tokens = torch.tensor(self.tokenizer.stop_tokens)

        for cur_pos in range(min_prompt_len, total_len):
            logits = self.model.forward(tokens[:, prev_pos:cur_pos], prev_pos)

            if temperature > 0:
                probs = torch.softmax(logits[:, -1] / temperature, dim=-1)
                next_token = sample_top_p(probs, top_p)
            else:
                next_token = torch.argmax(logits[:, -1], dim=-1)

            next_token = next_token.reshape(-1)
            # only replace token if prompt has already been generated
            next_token = torch.where(
                input_text_mask[:, cur_pos], tokens[:, cur_pos], next_token
            )
            tokens[:, cur_pos] = next_token

            target = tokens[:, prev_pos + 1 : cur_pos + 1]
            if logprobs:
                token_logprobs[:, prev_pos + 1 : cur_pos + 1] = -F.cross_entropy(
                    input=logits.transpose(1, 2),
                    target=tokens[:, prev_pos + 1 : cur_pos + 1],
                    reduction="none",
                    ignore_index=pad_id,
                )
            eos_reached |= (~input_text_mask[:, cur_pos]) & (
                torch.isin(next_token, stop_tokens)
            )
            yield TokenResult(
                token=next_token[0].item(),
                text=self.tokenizer.decode(next_token.tolist()),
                logprobs=(
                    token_logprobs[:, prev_pos + 1 : cur_pos + 1][0].tolist()
                    if logprobs
                    else None
                ),
            )

            prev_pos = cur_pos
            if all(eos_reached):
                break

    def text_completion(
        self,
        prompt: str,
        temperature: float = 0.6,
        top_p: float = 0.9,
        max_gen_len: Optional[int] = None,
        logprobs: bool = False,
        echo: bool = False,
    ) -> Generator:
        if (
            max_gen_len is None
            or max_gen_len == 0
            or max_gen_len >= self.model.params.max_seq_len
        ):
            max_gen_len = self.model.params.max_seq_len - 1

        prompt_tokens = self.tokenizer.encode(x, bos=True, eos=False)

        yield from self.generate(
            model_input=ModelInput(tokens=prompt_tokens),
            max_gen_len=max_gen_len,
            temperature=temperature,
            top_p=top_p,
            logprobs=logprobs,
            echo=echo,
        )

    def chat_completion(
        self,
        messages: List[Message],
        temperature: float = 0.6,
        top_p: float = 0.9,
        max_gen_len: Optional[int] = None,
        logprobs: bool = False,
    ) -> Generator:
        if (
            max_gen_len is None
            or max_gen_len == 0
            or max_gen_len >= self.model.params.max_seq_len
        ):
            max_gen_len = self.model.params.max_seq_len - 1

        yield from self.generate(
            model_input=self.formatter.encode_dialog_prompt(messages),
            max_gen_len=max_gen_len,
            temperature=temperature,
            top_p=top_p,
            logprobs=logprobs,
            include_stop_token=True,
        )


def sample_top_p(probs, p):
    """
    Perform top-p (nucleus) sampling on a probability distribution.

    Args:
        probs (torch.Tensor): Probability distribution tensor.
        p (float): Probability threshold for top-p sampling.

    Returns:
        torch.Tensor: Sampled token indices.

    Note:
        Top-p sampling selects the smallest set of tokens whose cumulative probability mass
        exceeds the threshold p. The distribution is renormalized based on the selected tokens.
    """
    probs_sort, probs_idx = torch.sort(probs, dim=-1, descending=True)
    probs_sum = torch.cumsum(probs_sort, dim=-1)
    mask = probs_sum - probs_sort > p
    probs_sort[mask] = 0.0
    probs_sort.div_(probs_sort.sum(dim=-1, keepdim=True))
    next_token = torch.multinomial(probs_sort, num_samples=1)
    next_token = torch.gather(probs_idx, -1, next_token)
    return next_token
