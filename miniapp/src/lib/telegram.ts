/**
 * SDK Telegram: тонкая обёртка над window.Telegram.WebApp.
 *
 * Сам скрипт подключён в index.html с адреса Telegram — он же выставляет
 * переменные темы --tg-theme-*, которыми красится приложение. Вне Telegram
 * объекта нет, и это нормальное состояние: приложение говорит об этом прямо,
 * а не показывает белый экран.
 */

export interface TelegramWebApp {
  initData: string;
  version: string;
  colorScheme: "light" | "dark";
  ready(): void;
  expand(): void;
}

declare global {
  interface Window {
    Telegram?: { WebApp?: TelegramWebApp };
  }
}

/** Объект Telegram, если приложение открыто внутри мессенджера. */
export function getWebApp(): TelegramWebApp | null {
  return window.Telegram?.WebApp ?? null;
}

/**
 * Сказать Telegram, что приложение готово, и развернуть его на весь экран.
 * Возвращает тот же объект — или null, если мы не в Telegram.
 */
export function initTelegram(): TelegramWebApp | null {
  const webApp = getWebApp();
  if (!webApp) {
    return null;
  }
  webApp.ready();
  webApp.expand();
  document.documentElement.style.colorScheme = webApp.colorScheme;
  return webApp;
}
