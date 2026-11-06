/**
 * 文件上传接口（/api/upload）：后端按后缀分流。
 * 图片 → 内存仓（ocr 通道，图片在 /api/ask 里直接走 multipart，不必用这里）；
 * txt / md → 落盘（read_file 通道），返回 path / content。
 */

export type UploadResult = {
  filename: string;
  size: number;
  accepted: boolean;
  channel: "ocr" | "read_file" | null;
  reason: string | null;
  image_id: string | null;
  path: string | null;
  content: string | null;
};

export async function uploadFile(file: { blob: Blob; name: string }): Promise<UploadResult> {
  const fd = new FormData();
  fd.append("file", file.blob, file.name);
  const res = await fetch("/api/upload", { method: "POST", body: fd });
  if (!res.ok) throw new Error("上传失败，请稍后重试。");
  return (await res.json()) as UploadResult;
}
