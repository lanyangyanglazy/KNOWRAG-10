const LEVEL_CLASS: Record<string, string> = {
  公开: "lv0",
  内部: "lv1",
  秘密: "lv2",
};

export function Badge({ classification }: { classification: string }) {
  const cls = LEVEL_CLASS[classification] ?? "lv2";
  return <span className={`badge ${cls}`}>{classification}</span>;
}
