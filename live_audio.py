"""Streaming mono PCM16 playback on the same exclusive channel as pack/preview audio."""
import asyncio
import io
import wave

from voicepack import PlaybackStopped


def wav_buffer(pcm, rate):
    data = io.BytesIO()
    with wave.open(data, 'wb') as wav:
        wav.setparams((1, 2, rate, 0, 'NONE', ''))
        wav.writeframes(pcm)
    data.seek(0)
    return data


class MixerSink:
    """SDL queues the next chunk before the previous one finishes."""
    def __init__(self, rate, volume):
        self.rate, self.volume = rate, volume
        self.channel = None

    async def write(self, pcm):
        import pygame
        if not pygame.mixer.get_init():
            pygame.mixer.init()
        sound = pygame.mixer.Sound(file=wav_buffer(pcm, self.rate))
        sound.set_volume(self.volume)
        while self.channel and self.channel.get_queue() is not None:
            await asyncio.sleep(.008)
        if self.channel and self.channel.get_busy():
            self.channel.queue(sound)
        else:
            self.channel = sound.play()
            if self.channel is None:
                raise RuntimeError('audio_channel_unavailable')

    async def drain(self):
        while self.channel and self.channel.get_busy():
            await asyncio.sleep(.01)

    def stop(self):
        if self.channel:
            self.channel.stop()


class LiveAudio:
    def __init__(self, player, volume=.55, sink_factory=MixerSink):
        self.player = player
        self.volume = volume
        self.sink_factory = sink_factory
        self.task = self.queue = self.response = None
        self.buffered = self.dropped = 0
        self.error = None

    @property
    def busy(self):
        return bool(self.task and not self.task.done())

    def start(self, response, rate, current, prelude, on_play=None):
        if self.busy or self.player.busy or type(rate) is not int or not 8000 <= rate <= 48000:
            return False
        self.response, self.rate = response, rate
        self.buffered = self.total = 0
        self.ended = False
        self.error = None
        self.queue = asyncio.Queue(96)
        self.task = asyncio.create_task(self._play(self.queue, rate, current, prelude, on_play))
        return True

    def start_speech(self, response, synthesize, current, prelude, on_play):
        if self.busy or self.player.busy:
            return False
        self.error = None
        self.response = response
        self.task = asyncio.create_task(self._speech(synthesize, current, prelude, on_play))
        return True

    async def _speech(self, synthesize, current, prelude, on_play):
        try:
            async with self.player.sequence():
                if not current():
                    return
                data = await asyncio.wait_for(synthesize(), 20)
                if data and current():
                    await prelude()
                    if current():
                        await self.player.play(io.BytesIO(data[0]), self.volume, on_play)
        except PlaybackStopped:
            pass
        except Exception as error:
            self.error = type(error).__name__

    async def feed_stream(self, response, data):
        # A provider can deliver several seconds of PCM in a single burst.
        while (response == self.response and self.busy and self.queue is not None
               and isinstance(data, bytes) and 0 < len(data) <= self.rate
               and (self.queue.qsize() >= self.queue.maxsize - 1 or self.buffered + len(data) > self.rate * 4)):
            await asyncio.sleep(.01)
        return self.feed(response, data)

    def feed(self, response, data):
        if self.queue is None or not self.busy or response != self.response or self.ended:
            return False
        if (not isinstance(data, bytes) or not data or len(data) % 2 or len(data) > self.rate
                or self.buffered + len(data) > self.rate * 4 or self.queue.full()
                or self.total + len(data) > self.rate * 30):
            self.dropped += 1
            self.cancel()
            return False
        self.queue.put_nowait(data)
        self.buffered += len(data)
        self.total += len(data)
        return True

    def end(self, response):
        if self.queue is not None and self.busy and response == self.response and not self.ended:
            self.ended = True
            if self.queue.full():
                self.cancel()
            else:
                self.queue.put_nowait(None)

    def cancel(self):
        if self.busy:
            self.task.cancel()
        self.queue = self.response = None
        self.buffered = 0

    async def _play(self, queue, rate, current, prelude, on_play):
        sink = None
        try:
            async with self.player.sequence():
                if not current():
                    return
                # Wait for actual audio before sounding the alarm.
                chunk = await asyncio.wait_for(queue.get(), 3)
                if chunk is None or not current():
                    return
                await prelude()
                if not current():
                    return
                sink = self.sink_factory(rate, self.volume)
                if on_play:
                    on_play()
                while chunk is not None and current():
                    self.buffered -= len(chunk)
                    await sink.write(chunk)
                    chunk = await asyncio.wait_for(queue.get(), 3)
                if current():
                    await sink.drain()
        except PlaybackStopped:
            pass
        except Exception as error:
            self.error = type(error).__name__
        finally:
            if sink:
                sink.stop()
            self.response = self.queue = None
            self.buffered = 0

    async def close(self):
        self.cancel()
        if self.task:
            await asyncio.gather(self.task, return_exceptions=True)

