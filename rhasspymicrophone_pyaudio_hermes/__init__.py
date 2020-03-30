"""Hermes MQTT server for Rhasspy TTS using external program"""
import io
import logging
import socket
import threading
import time
import typing
import wave
from queue import Queue

import pyaudio
import webrtcvad
from rhasspyhermes.asr import AsrStartListening, AsrStopListening
from rhasspyhermes.audioserver import (
    AudioDevice,
    AudioDeviceMode,
    AudioDevices,
    AudioFrame,
    AudioGetDevices,
    AudioSummary,
    SummaryToggleOff,
    SummaryToggleOn,
)
from rhasspyhermes.base import Message
from rhasspyhermes.client import GeneratorType, HermesClient

_LOGGER = logging.getLogger(__name__)


class MicrophoneHermesMqtt(HermesClient):
    """Hermes MQTT server for Rhasspy microphone input using external program."""

    def __init__(
        self,
        client,
        sample_rate: int,
        sample_width: int,
        channels: int,
        device_index: typing.Optional[int] = None,
        chunk_size: int = 2048,
        siteIds: typing.Optional[typing.List[str]] = None,
        output_siteId: typing.Optional[str] = None,
        udp_audio_port: typing.Optional[int] = None,
        vad_mode: int = 3,
    ):
        super().__init__(
            "rhasspymicrophone_pyaudio_hermes",
            client,
            sample_rate=sample_rate,
            sample_width=sample_width,
            channels=channels,
            siteIds=siteIds,
        )

        self.subscribe(AudioGetDevices, SummaryToggleOn, SummaryToggleOff)

        self.sample_rate = sample_rate
        self.sample_width = sample_width
        self.channels = channels
        self.device_index = device_index
        self.frames_per_buffer = chunk_size // sample_width
        self.output_siteId = output_siteId or self.siteId

        self.udp_audio_port = udp_audio_port
        self.udp_output = False
        self.udp_socket: typing.Optional[socket.socket] = None

        self.chunk_queue: Queue = Queue()

        # Send audio summaries
        self.enable_summary = False
        self.vad: typing.Optional[webrtcvad.Vad] = None
        self.vad_mode = vad_mode
        self.vad_audio_data = bytes()
        self.vad_chunk_size: int = 960  # 30ms

        # Start threads
        if self.udp_audio_port is not None:
            self.udp_output = True
            self.udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            _LOGGER.debug("Audio will also be sent to UDP port %s", self.udp_audio_port)
            self.subscribe(AsrStartListening, AsrStopListening)

        threading.Thread(target=self.publish_chunks, daemon=True).start()
        threading.Thread(target=self.record, daemon=True).start()

    # -------------------------------------------------------------------------

    def record(self):
        """Record audio from PyAudio device."""
        try:
            audio = pyaudio.PyAudio()

            # Open device
            mic = audio.open(
                input_device_index=self.device_index,
                channels=self.channels,
                format=audio.get_format_from_width(self.sample_width),
                rate=self.sample_rate,
                input=True,
            )

            assert mic is not None
            mic.start_stream()
            _LOGGER.debug("Recording audio")

            try:
                # Read frames and publish as MQTT WAV chunks
                while True:
                    chunk = mic.read(self.frames_per_buffer)
                    if chunk:
                        self.chunk_queue.put(chunk)
                    else:
                        # Avoid 100% CPU
                        time.sleep(0.01)
            finally:
                mic.stop_stream()
                audio.terminate()

        except Exception:
            _LOGGER.exception("record")

    def publish_chunks(self):
        """Publish audio chunks to MQTT or UDP."""
        try:
            udp_dest = ("127.0.0.1", self.udp_audio_port)

            while True:
                chunk = self.chunk_queue.get()
                if chunk:
                    # MQTT output
                    with io.BytesIO() as wav_buffer:
                        wav_file: wave.Wave_write = wave.open(wav_buffer, "wb")
                        with wav_file:
                            wav_file.setframerate(self.sample_rate)
                            wav_file.setsampwidth(self.sample_width)
                            wav_file.setnchannels(self.channels)
                            wav_file.writeframes(chunk)

                        wav_bytes = wav_buffer.getvalue()

                        if self.udp_output:
                            # UDP output
                            self.udp_socket.sendto(wav_bytes, udp_dest)
                        else:
                            # Publish to output siteId
                            self.publish(
                                AudioFrame(wav_bytes=wav_bytes),
                                siteId=self.output_siteId,
                            )

                    if self.enable_summary:
                        if not self.vad:
                            # Create voice activity detector
                            self.vad = webrtcvad.Vad()
                            self.vad.set_mode(self.vad_mode)

                        # webrtcvad needs 16-bit 16Khz mono
                        self.vad_audio_data += self.maybe_convert_wav(
                            wav_bytes, sample_rate=16000, sample_width=2, channels=1
                        )

                        is_speech = False

                        # Process in chunks of 30ms for webrtcvad
                        while len(self.vad_audio_data) >= self.vad_chunk_size:
                            vad_chunk = self.vad_audio_data[: self.vad_chunk_size]
                            self.vad_audio_data = self.vad_audio_data[
                                self.vad_chunk_size :
                            ]

                            # Speech in any chunk counts as speech
                            is_speech = is_speech or self.vad.is_speech(
                                vad_chunk, 16000
                            )

                        # Publish audio summary
                        self.publish(
                            AudioSummary(
                                debiased_energy=AudioSummary.get_debiased_energy(chunk),
                                is_speech=is_speech,
                            ),
                            siteId=self.output_siteId,
                        )

        except Exception:
            _LOGGER.exception("publish_chunks")

    async def handle_get_devices(
        self, get_devices: AudioGetDevices
    ) -> typing.AsyncIterable[AudioDevices]:
        """Get available microphones and optionally test them."""
        if get_devices.modes and (AudioDeviceMode.INPUT not in get_devices.modes):
            _LOGGER.debug("Not a request for input devices")
            return

        devices: typing.List[AudioDevice] = []

        try:
            audio = pyaudio.PyAudio()

            default_name = audio.get_default_input_device_info().get("name")
            for device_index in range(audio.get_device_count()):
                device_info = audio.get_device_info_by_index(device_index)
                device_name = device_info.get("name")
                if device_name == default_name:
                    device_name += "*"

                working: typing.Optional[bool] = None
                if get_devices.test:
                    working = self.get_microphone_working(
                        device_name, device_index, audio
                    )

                devices.append(
                    AudioDevice(
                        mode=AudioDeviceMode.INPUT,
                        id=str(device_index),
                        name=device_name,
                        description="",
                        working=working,
                    )
                )
        except Exception:
            _LOGGER.exception("handle_get_devices")
        finally:
            audio.terminate()

        yield AudioDevices(
            devices=devices, id=get_devices.id, siteId=get_devices.siteId
        )

    def get_microphone_working(
        self,
        device_name: str,
        device_index: int,
        audio: pyaudio.PyAudio,
        chunk_size: int = 1024,
    ) -> bool:
        """Record some audio from a microphone and check its energy."""
        try:
            # read audio
            pyaudio_stream = audio.open(
                input_device_index=device_index,
                channels=self.channels,
                format=audio.get_format_from_width(self.sample_width),
                rate=self.sample_rate,
                input=True,
            )

            try:
                audio_data = pyaudio_stream.read(chunk_size)
                if not pyaudio_stream.is_stopped():
                    pyaudio_stream.stop_stream()
            finally:
                pyaudio_stream.close()

            debiased_energy = AudioSummary.get_debiased_energy(audio_data)

            # probably actually audio
            return debiased_energy > 30
        except Exception:
            _LOGGER.exception("get_microphone_working ({device_name})")
            pass

        return False

    # -------------------------------------------------------------------------

    async def on_message(
        self,
        message: Message,
        siteId: typing.Optional[str] = None,
        sessionId: typing.Optional[str] = None,
        topic: typing.Optional[str] = None,
    ) -> GeneratorType:
        """Received message from MQTT broker."""
        if isinstance(message, AudioGetDevices):
            async for device_result in self.handle_get_devices(message):
                yield device_result
        elif isinstance(message, AsrStartListening):
            if self.udp_audio_port is not None:
                self.udp_output = False
                _LOGGER.debug("Disable UDP output")
        elif isinstance(message, AsrStopListening):
            if self.udp_audio_port is not None:
                self.udp_output = True
                _LOGGER.debug("Enable UDP output")
        elif isinstance(message, SummaryToggleOn):
            self.enable_summary = True
            _LOGGER.debug("Enable audio summaries")
        elif isinstance(message, SummaryToggleOff):
            self.enable_summary = False
            _LOGGER.debug("Disable audio summaries")
        else:
            _LOGGER.warning("Unexpected message: %s", message)
