/**
 * Нижние вкладки «Сегодня · Неделя · Сферы · Дела» — корень приложения
 * (`design.md` §1, `techspec/32-redesign.md` §32.2). Открывается «Сегодня».
 * Внутри карточки дела, «О себе» и «Оформления» вкладок нет — там кнопка
 * «назад» Telegram. Активная вкладка — цветом действия темы.
 */
import type { ReactNode } from "react";

export type Tab = "today" | "week" | "spheres" | "tasks";

/** Иконки — как на артбордах прототипа: штрих в currentColor, 24px. */
const ICONS: Record<Tab, ReactNode> = {
  today: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round">
      <circle cx="12" cy="12" r="4" />
      <path d="M12 2.5v2.2M12 19.3v2.2M2.5 12h2.2M19.3 12h2.2M5.3 5.3l1.6 1.6M17.1 17.1l1.6 1.6M5.3 18.7l1.6-1.6M17.1 6.9l1.6-1.6" />
    </svg>
  ),
  week: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round">
      <path d="M3.5 6h9M8 12h11M5.5 18h7" />
    </svg>
  ),
  spheres: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8">
      <circle cx="8.5" cy="8.5" r="4.5" />
      <circle cx="15.5" cy="8.5" r="4.5" />
      <circle cx="12" cy="15.5" r="4.5" />
    </svg>
  ),
  tasks: (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="M4 6.5l1.6 1.6L8.5 5M4 13l1.6 1.6L8.5 11.5M4 19.5h.01M11.5 7h8.5M11.5 13.5h8.5M11.5 19.5h8.5" />
    </svg>
  ),
};

export const TABS: readonly { key: Tab; label: string }[] = [
  { key: "today", label: "Сегодня" },
  { key: "week", label: "Неделя" },
  { key: "spheres", label: "Сферы" },
  { key: "tasks", label: "Дела" },
];

export function Tabs({ active, onChange }: { active: Tab; onChange: (tab: Tab) => void }) {
  return (
    <nav className="tabs" aria-label="Разделы">
      {TABS.map((tab) => (
        <button
          key={tab.key}
          type="button"
          className={tab.key === active ? "tab tab--on" : "tab"}
          aria-current={tab.key === active ? "page" : undefined}
          onClick={() => onChange(tab.key)}
        >
          <span className="tab__ic" aria-hidden="true">
            {ICONS[tab.key]}
          </span>
          <span>{tab.label}</span>
        </button>
      ))}
    </nav>
  );
}
