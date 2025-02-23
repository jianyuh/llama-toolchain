# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the terms described in the LICENSE file in
# the root directory of this source tree.

from termcolor import cprint

from llama_toolchain.inference.api import ChatCompletionResponseEventType


class LogEvent:
    def __init__(
        self,
        content: str = "",
        end: str = "\n",
        color="white",
    ):
        self.content = content
        self.color = color
        self.end = "\n" if end is None else end

    def print(self, flush=True):
        cprint(f"{self.content}", color=self.color, end=self.end, flush=flush)


class EventLogger:
    async def log(self, event_generator, stream=True):
        async for chunk in event_generator:
            event = chunk.event
            if event.event_type == ChatCompletionResponseEventType.start:
                yield LogEvent("Assistant> ", color="cyan", end="")
            elif event.event_type == ChatCompletionResponseEventType.progress:
                yield LogEvent(event.delta, color="yellow", end="")
            elif event.event_type == ChatCompletionResponseEventType.complete:
                yield LogEvent("")
