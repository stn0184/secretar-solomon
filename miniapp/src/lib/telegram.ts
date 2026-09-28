/**
 * SDK Telegram: тонкая обёртка над window.Telegram.WebApp.
 *
 * Сам скрипт подключён в index.html с адреса Telegram — он же выставляет
 * переменные темы --tg-theme-*, которыми красится приложение. Вне Telegram
 * объекта нет, и это нормальное состояние: приложение говорит об этом прямо,
 * а не показывает белый экран.
 *
 * Компоненты `window.Telegram` не трогают: всё, что им нужно от мессенджера,
 * — здесь, функциями без React.
 */

interface PopupButton {
  id?: string;
  type?: "default" | "ok" | "close" | "cancel" | "destructive";
  text?: string;
}

interface PopupParams {
  title?: string;
  message: string;
  buttons?: PopupButton[];
}

interface BackButton {
  isVisible: boolean;
  show(): void;
  hide(): void;
  onClick(callback: () => void): void;
  offClick(callback: () => void): void;
}

export interface TelegramWebApp {
  initData: string;
  version: string;
  colorScheme: "light" | "dark";
  BackButton: BackButton;
  ready(): void;
  expand(): void;
  close(): void;
  isVersionAtLeast(version: string): boolean;
  showPopup(params: PopupParams, callback?: (buttonId: string) => void): void;
  onEvent(event: "themeChanged", callback: () => void): void;
  offEvent(event: "themeChanged", callback: () => void): void;
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

/** Схема темы — атрибутом на <html>: по нему CSS выбирает фиксированные пары цветов. */
function applyScheme(scheme: "light" | "dark"): void {
  document.documentElement.dataset.scheme = scheme;
  document.documentElement.style.colorScheme = scheme;
}

/**
 * Сказать Telegram, что приложение готово, развернуть его на весь экран
 * и следить за сменой темы. Возвращает отписку от события темы.
 * Вне Telegram схема берётся у браузера, чтобы dev-прогон выглядел как надо.
 */
export function initTelegram(): () => void {
  const webApp = getWebApp();
  if (!webApp) {
    applyScheme(window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
    return () => {};
  }
  webApp.ready();
  webApp.expand();
  const onTheme = () => applyScheme(webApp.colorScheme);
  onTheme();
  webApp.onEvent("themeChanged", onTheme);
  return () => webApp.offEvent("themeChanged", onTheme);
}

/** `initData` Telegram — подпись, которую обменивают на токен. Пусто вне Telegram. */
export function getInitData(): string {
  return getWebApp()?.initData ?? "";
}

/**
 * Показать кнопку «назад» Telegram и повесить на неё обработчик.
 * Возвращает отписку: спрятать кнопку и снять обработчик. Вне Telegram
 * и на SDK старше 6.1 — ничего не делает: остаётся своя стрелка.
 */
export function showBackButton(onClick: () => void): () => void {
  const webApp = getWebApp();
  if (!webApp || !webApp.isVersionAtLeast("6.1")) {
    return () => {};
  }
  webApp.BackButton.onClick(onClick);
  webApp.BackButton.show();
  return () => {
    webApp.BackButton.offClick(onClick);
    webApp.BackButton.hide();
  };
}

/** Длиннее — в системное окно не влезет: у showPopup предел 256 знаков на текст. */
const TITLE_IN_POPUP = 80;

function shorten(text: string): string {
  return text.length > TITLE_IN_POPUP ? `${text.slice(0, TITLE_IN_POPUP - 1)}…` : text;
}

/**
 * Подтверждение необратимого действия — системным окном Telegram
 * (`showPopup`), не своим. Вне Telegram и на SDK старше 6.2 — окно
 * браузера, чтобы dev-прогон жил. `button` — слово на красной кнопке:
 * то, что случится («Удалить», «Бросить»).
 */
function confirmDestructive(title: string, message: string, button: string): Promise<boolean> {
  const webApp = getWebApp();
  if (!webApp || !webApp.isVersionAtLeast("6.2")) {
    return Promise.resolve(window.confirm(`${title}\n\n${message}`));
  }
  return new Promise((resolve) => {
    webApp.showPopup(
      {
        title,
        message,
        buttons: [
          { id: "cancel", type: "cancel" },
          { id: "confirm", type: "destructive", text: button },
        ],
      },
      (buttonId) => resolve(buttonId === "confirm"),
    );
  });
}

/** Удалить задачу: вместе с напоминаниями, сообщение в переписке остаётся. */
export function confirmDelete(title: string): Promise<boolean> {
  return confirmDestructive(
    "Удалить задачу?",
    `«${shorten(title)}» исчезнет вместе с напоминаниями. Сообщение в переписке останется.`,
    "Удалить",
  );
}

/** Удалить запись памяти: помощник её забудет, сообщение в переписке остаётся. */
export function confirmRemoveFact(text: string): Promise<boolean> {
  return confirmDestructive(
    "Удалить запись?",
    `«${shorten(text)}» исчезнет из памяти помощника. Сообщение в переписке останется.`,
    "Удалить",
  );
}

/** Уйти из формы правки с несохранёнными изменениями: они пропадут. */
export function confirmDiscard(): Promise<boolean> {
  return confirmDestructive("Бросить правку?", "Изменения не сохранятся.", "Бросить");
}

/** Закрыть приложение и вернуться в чат. Вне Telegram закрывать нечего. */
export function closeApp(): void {
  getWebApp()?.close();
}
