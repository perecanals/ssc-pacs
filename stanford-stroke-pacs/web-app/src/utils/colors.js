export const NOTION_COLORS = [
  { bg: "#f3e8ff", text: "#7c3aed" },
  { bg: "#dbeafe", text: "#2563eb" },
  { bg: "#dcfce7", text: "#16a34a" },
  { bg: "#fef3c7", text: "#d97706" },
  { bg: "#ffe4e6", text: "#e11d48" },
  { bg: "#ffedd5", text: "#ea580c" },
  { bg: "#e0f2fe", text: "#0284c7" },
  { bg: "#e0e7ff", text: "#4f46e5" },
  { bg: "#fce7f3", text: "#be185d" },
  { bg: "#ccfbf1", text: "#0d9488" },
];

export function hashStr(s) {
  let h = 0;
  for (let i = 0; i < s.length; i++) h = ((h << 5) - h + s.charCodeAt(i)) | 0;
  return Math.abs(h);
}

// A timepoint can carry a sequence number or a qualifier (BLT1, BLO_MR).
// Older follow-ups also put the interval first, as in 24h_FU and 5d_FU.
const TIMEPOINT_COLORS = {
  BL: { bg: "#dcfce7", text: "#166534" },
  BLO: { bg: "#ecfccb", text: "#456413" },
  BLT: { bg: "#d1fae5", text: "#00664b" },
  THROMBECTOMY: { bg: "#dbeafe", text: "#1d4ed8" },
  PRE: { bg: "#ffedd5", text: "#92400e", border: "#fbad70" },
  FU: { bg: "#fee2e2", text: "#b91c1c" },
};

export function valueColor(value, labelName) {
  if (labelName === "timepoint") {
    const tokens = String(value)
      .trim()
      .toUpperCase()
      .split(/[_ -]+/);
    for (const token of tokens) {
      const family = token.replace(/\d+$/, "");
      if (Object.hasOwn(TIMEPOINT_COLORS, family))
        return TIMEPOINT_COLORS[family];
    }
  }
  return NOTION_COLORS[hashStr(value) % NOTION_COLORS.length];
}
