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

export function valueColor(value) {
  return NOTION_COLORS[hashStr(value) % NOTION_COLORS.length];
}
