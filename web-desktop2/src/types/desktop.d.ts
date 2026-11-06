/* 桌面端 preload 暴露的少量能力（浏览器里没有，按可选处理）。 */
export {};

declare global {
  /** 构建时由 vite 注入的版本号 */
  const __APP_VERSION__: string;

  interface Window {
    glyphDesktop?: {
      openLogs: () => Promise<string>;
      logPath: () => Promise<string>;
    };
  }
}
