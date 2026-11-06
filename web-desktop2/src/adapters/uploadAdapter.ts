/**
 * 上传适配器：未接后端时一律用本地 blob URL。
 *
 * 为什么必须是本地 blob：批注图要把原图和批注层合成导出，
 * 若原图来自 `/api` 返回的跨域 URL，canvas 会被污染，`toDataURL` 直接抛 SecurityError。
 */

export type Uploaded = {
  id: string;
  name: string;
  url: string;
  blob: Blob;
  size: number;
  kind: "image" | "file";
};

export function upload(file: File): Uploaded {
  const isImage = file.type.startsWith("image/");
  return {
    id: `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 7)}`,
    name: file.name,
    url: URL.createObjectURL(file),
    blob: file,
    size: file.size,
    kind: isImage ? "image" : "file",
  };
}

export function release(u: Uploaded) {
  URL.revokeObjectURL(u.url);
}

export function humanSize(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}
