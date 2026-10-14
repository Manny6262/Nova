"""Bounded endpoint scheduling and playback-reference echo reduction.

Smart Turn predicts completion; silence is only the fail-safe. This filter is
not full room AEC: reverberation and nonlinear speakers can leave residual echo.
"""
from collections import deque
import numpy as np

class EndpointSchedule:
    def __init__(self): self.next_check = .65
    def speech(self): self.next_check = .65
    def check(self, silence):
        if silence < self.next_check: return False
        self.next_check = silence + .45
        return True
    def finish(self, silence, complete=False, duration=0):
        return complete or silence >= 3.5 or duration >= 90

class PlaybackEchoFilter:
    def __init__(self): self.reference = deque(maxlen=32)
    def add_reference(self, pcm):
        self.reference.append(np.frombuffer(pcm, dtype='<i2').astype(np.float32))
    def clean(self, pcm):
        mic = np.frombuffer(pcm, dtype='<i2').astype(np.float32)
        if not self.reference or not len(mic): return pcm
        ref = np.concatenate(tuple(self.reference))
        n = len(mic)
        if len(ref) < n: return pcm
        best, match, gain = 0., None, 0.
        # Fit a recent delayed playback window. A fit removes the correlated
        # component rather than muting the whole frame, retaining mixed speech.
        energy = float(np.dot(mic[::4], mic[::4])) + 1
        for end in range(len(ref), n-1, -160):
            candidate = ref[end-n:end]
            short = candidate[::4]
            ref_energy = float(np.dot(short, short)) + 1
            cross = float(np.dot(mic[::4], short))
            correlation = abs(cross) / (energy * ref_energy) ** .5
            if correlation > best:
                best, match, gain = correlation, candidate, cross/ref_energy
        if match is None or best < .45: return pcm
        residual = mic - np.clip(gain, -2., 2.) * match
        return np.clip(residual, -32768, 32767).astype('<i2').tobytes()
