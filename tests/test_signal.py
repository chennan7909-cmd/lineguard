"""Signal layer: z-gate, absolute-move gate, cooldown, goal attribution."""
from lineguard.signal.detector import DetectorConfig, MovementDetector
from tests._helpers import mk_odds

BASE = [(0.440, 0.28, 0.28), (0.442, 0.279, 0.279), (0.438, 0.281, 0.281),
        (0.441, 0.2795, 0.2795), (0.439, 0.2805, 0.2805), (0.443, 0.2785, 0.2785)]


def warm(det, fid, t0):
    for k in range(6):
        det.on_odds(mk_odds(fid, t0 + k * 60_000, BASE[k], f"{fid}w{k}"))


def test_small_absolute_move_below_delta_gate_no_signal():
    det = MovementDetector(DetectorConfig(min_window=3))
    warm(det, 1, 1_000_000)
    assert det.on_odds(mk_odds(1, 1_360_000, (0.465, 0.27, 0.265), "s")) == []  # +2.5% < 4%


def test_dual_gate_fires_and_cooldown_suppresses_repeat():
    det = MovementDetector(DetectorConfig(min_window=3))
    warm(det, 1, 1_000_000)
    assert det.on_odds(mk_odds(1, 1_360_000, (0.53, 0.24, 0.23), "s1"))
    assert det.on_odds(mk_odds(1, 1_420_000, (0.55, 0.23, 0.22), "s2")) == []   # cooldown


def test_score_odds_timestamp_attribution():
    det = MovementDetector(DetectorConfig(min_window=3))
    t0 = 1_000_000
    base = [(0.440, 0.28, 0.28), (0.442, 0.279, 0.279), (0.438, 0.281, 0.281),
            (0.441, 0.2795, 0.2795), (0.439, 0.2805, 0.2805), (0.443, 0.2785, 0.2785)]
    for k in range(6):
        det.on_odds(mk_odds(1, t0 + k * 60_000, base[k], f"w{k}"))
    det.note_score_event(1, t0 + 6 * 60_000 - 30_000, "goal")      # 30s before spike
    sigs = det.on_odds(mk_odds(1, t0 + 6 * 60_000, (0.53, 0.24, 0.23), "spike"))
    assert sigs and sigs[0].event_driven is True
    det2 = MovementDetector(DetectorConfig(min_window=3))
    for k in range(6):
        det2.on_odds(mk_odds(2, t0 + k * 60_000, base[k], f"x{k}"))
    det2.note_score_event(2, t0, "goal")                            # 6 min before -> outside window
    sigs2 = det2.on_odds(mk_odds(2, t0 + 6 * 60_000, (0.53, 0.24, 0.23), "spike2"))
    assert sigs2 and sigs2[0].event_driven is False
