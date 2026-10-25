/**
 * Matrix multi-select rules: same court, contiguous 30-min slots, total ≤ 2h.
 * Exposed as window.MatrixTarget for index.html and node tests.
 */
(() => {
  const MAX_TARGET_MINUTES = 120;
  const SLOT_STEP = 30;

  const hm2min = (t) => {
    const [h, m] = String(t).split(":").map(Number);
    return h * 60 + m;
  };

  const min2hm = (m) => {
    const hh = String(Math.floor(m / 60)).padStart(2, "0");
    const mm = String(m % 60).padStart(2, "0");
    return `${hh}:${mm}`;
  };

  function normalizeSlot(slot) {
    return {
      court: Number(slot.court),
      start: String(slot.start),
      end: String(slot.end),
      date: String(slot.date || ""),
    };
  }

  function slotDurationMinutes(slot) {
    const d = hm2min(slot.end) - hm2min(slot.start);
    return d > 0 ? d : SLOT_STEP;
  }

  function sortSlots(slots) {
    return [...slots].sort(
      (a, b) => hm2min(a.start) - hm2min(b.start) || a.court - b.court,
    );
  }

  function totalMinutes(slots) {
    return slots.reduce((sum, s) => sum + slotDurationMinutes(s), 0);
  }

  function sameCourt(slots) {
    if (!slots.length) return true;
    const c = slots[0].court;
    return slots.every((s) => s.court === c);
  }

  function isContiguous(slots) {
    if (slots.length <= 1) return true;
    const ordered = sortSlots(slots);
    for (let i = 1; i < ordered.length; i++) {
      if (ordered[i].court !== ordered[0].court) return false;
      if (hm2min(ordered[i].start) !== hm2min(ordered[i - 1].end)) return false;
    }
    return true;
  }

  function validateSelection(slots) {
    if (!slots.length) return { ok: true, slots: [] };
    const normalized = slots.map(normalizeSlot);
    if (!sameCourt(normalized)) {
      return { ok: false, code: "cross_court", msg: "不能跨场多选，请只选择同一场地的连续时段。" };
    }
    if (!isContiguous(normalized)) {
      return { ok: false, code: "non_contiguous", msg: "时段必须连续，不能跳格选择。" };
    }
    const total = totalMinutes(normalized);
    if (total > MAX_TARGET_MINUTES) {
      return {
        ok: false,
        code: "over_limit",
        msg: `所选时段合计 ${total} 分钟，超过 2 小时上限（120 分钟）。`,
      };
    }
    return { ok: true, slots: sortSlots(normalized) };
  }

  function tryAdd(current, slot) {
    const next = sortSlots([...current.map(normalizeSlot), normalizeSlot(slot)]);
    const v = validateSelection(next);
    if (!v.ok) return v;
    return { ok: true, slots: v.slots };
  }

  function collapseRange(slots) {
    const v = validateSelection(slots);
    if (!v.ok || !v.slots.length) return null;
    const ordered = v.slots;
    return {
      court: ordered[0].court,
      start: ordered[0].start,
      end: ordered[ordered.length - 1].end,
      date: ordered[0].date,
      durationMinutes: totalMinutes(ordered),
    };
  }

  const api = {
    MAX_TARGET_MINUTES,
    SLOT_STEP,
    hm2min,
    min2hm,
    normalizeSlot,
    sortSlots,
    totalMinutes,
    validateSelection,
    tryAdd,
    collapseRange,
  };

  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
  if (typeof window !== "undefined") {
    window.MatrixTarget = api;
  }
})();
