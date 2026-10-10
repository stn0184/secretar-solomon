/**
 * SDK Telegram: тонкая обёртка над window.Telegram.WebApp.
 *
 * Сам скрипт подключён в index.html с адреса Telegram. С редизайна
 * (`design.md` §1) цвета и шрифты приложения свои: от Telegram берётся только
 * схема — светлая или тёмная, — по ней выбирается тема «Как в Telegram», а
 * шапка и фон мессенджера красятся в фон выбранной темы. Вне Telegram
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

/** Кнопка «назад» в шапке и пункт «Настройки» в меню ⋯ — одно устройство. */
interface MenuButton {
  isVisible: boolean;
  show(): void;
  hide(): void;
  onClick(callback: () => void): void;
  offClick(callback: () => void): void;
}

export type ColorScheme = "light" | "dark";

export interface TelegramWebApp {
  initData: string;
  version: string;
  colorScheme: ColorScheme;
  BackButton: MenuButton;
  /** Пункт «Настройки» в меню ⋯ (Bot API 7.0). */
  SettingsButton?: MenuButton;
  ready(): void;
  expand(): void;
  close(): void;
  isVersionAtLeast(version: string): boolean;
  showPopup(params: PopupParams, callback?: (buttonId: string) => void): void;
  setHeaderColor(color: string): void;
  setBackgroundColor(color: string): void;
  setBottomBarColor?(color: string): void;
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

/** Сказать Telegram, что приложение готово, и развернуть его на весь экран. */
export function initTelegram(): void {
  const webApp = getWebApp();
  if (!webApp) {
    return;
  }
  webApp.ready();
  webApp.expand();
}

const DARK_QUERY = "(prefers-color-scheme: dark)";

/**
 * Схема Telegram — светлая или тёмная. Вне Telegram — схема браузера,
 * чтобы dev-прогон выглядел так же, как в мессенджере с той же схемой.
 */
export function getColorScheme(): ColorScheme {
  const webApp = getWebApp();
  if (webApp) {
    return webApp.colorScheme === "dark" ? "dark" : "light";
  }
  return window.matchMedia(DARK_QUERY).matches ? "dark" : "light";
}

/**
 * Следить за сменой схемы: в Telegram — событие `themeChanged` (переключили
 * ночной режим), вне его — схема браузера. Возвращает отписку.
 */
export function onColorScheme(callback: (scheme: ColorScheme) => void): () => void {
  const webApp = getWebApp();
  if (webApp) {
    const onTheme = () => callback(getColorScheme());
    webApp.onEvent("themeChanged", onTheme);
    return () => webApp.offEvent("themeChanged", onTheme);
  }
  const query = window.matchMedia(DARK_QUERY);
  const onChange = () => callback(getColorScheme());
  query.addEventListener("change", onChange);
  return () => query.removeEventListener("change", onChange);
}

/**
 * Шапка, фон под приложением и нижняя полоса Android — в фон темы:
 * мессенджер вокруг приложения не спорит с ним цветом. Любой цвет шапки
 * Telegram принимает с 6.9, нижнюю полосу — с 7.10; старее — как есть.
 */
export function setChromeColor(color: string): void {
  const webApp = getWebApp();
  if (!webApp || !webApp.isVersionAtLeast("6.9")) {
    return;
  }
  webApp.setHeaderColor(color);
  webApp.setBackgroundColor(color);
  if (webApp.isVersionAtLeast("7.10")) {
    webApp.setBottomBarColor?.(color);
  }
}

/** Есть ли кнопка «назад» Telegram: нет — экран показывает свою стрелку. */
export function hasBackButton(): boolean {
  const webApp = getWebApp();
  return webApp !== null && webApp.isVersionAtLeast("6.1");
}

/**
 * Пункт «Настройки» в меню ⋯ Telegram — вход в «Оформление» (`design.md` §1).
 * Возвращает отписку: спрятать пункт и снять обработчик. Вне Telegram и на
 * SDK старше 7.0 пункта нет — ничего не делает.
 */
export function showSettingsButton(onClick: () => void): () => void {
  const webApp = getWebApp();
  const button = webApp?.SettingsButton;
  if (!webApp || !button || !webApp.isVersionAtLeast("7.0")) {
    return () => {};
  }
  button.onClick(onClick);
  button.show();
  return () => {
    button.offClick(onClick);
    button.hide();
  };
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
  if (!webApp || !hasBackButton()) {
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

/**
 * Слова окна удаления. У повторяющейся задачи удаляется вся цепочка —
 * и этот раз, и все следующие (§13.6), окно говорит об этом прямо.
 */
export function deleteQuestion(
  title: string,
  repeating: boolean,
): { title: string; message: string } {
  const gone = `«${shorten(title)}» исчезнет вместе с напоминаниями`;
  return repeating
    ? {
        title: "Удалить задачу со всеми повторами?",
        message: `${gone} — и этот раз, и все следующие. Сообщение в переписке останется.`,
      }
    : { title: "Удалить задачу?", message: `${gone}. Сообщение в переписке останется.` };
}

/** Удалить задачу: вместе с напоминаниями, сообщение в переписке остаётся. */
export function confirmDelete(title: string, repeating = false): Promise<boolean> {
  const question = deleteQuestion(title, repeating);
  return confirmDestructive(question.title, question.message, "Удалить");
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
