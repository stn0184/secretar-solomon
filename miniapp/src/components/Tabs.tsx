/**
 * Нижние вкладки «Задачи | О себе» — корень приложения (`design.md` §1).
 * Есть на обоих корневых экранах; внутри карточки задачи их нет, там
 * кнопка «назад» Telegram. Активная вкладка — цветом действия.
 */
import type { ReactNode } from "react";

export type Tab = "tasks" | "me";

const ICON_TASKS = (
  <svg
    viewBox="0 0 22 22"
    fill="none"
    stroke="currentColor"
    strokeWidth="1.7"
    strokeLinecap="round"
    strokeLinejoin="round"
    aria-hidden="true"
  >
    <path d="M8.5 5.5h11M8.5 11h11M8.5 16.5h7.5" />
    <path d="M2.5 5.2 3.9 6.6 6.4 4" />
    <path d="M2.5 10.7 3.9 12.1 6.4 9.5" />
  </svg>
);

const ICON_ME = (
  <svg
    viewBox="0 0 22 22"
    fill="none"
    stroke="currentColor"
    strokeWidth="1.7"
    strokeLinecap="round"
    strokeLinejoin="round"
    aria-hidden="true"
  >
    <circle cx="11" cy="7.6" r="3.7" />
    <path d="M4.2 19c.4-3.7 3.2-6 6.8-6s6.4 2.3 6.8 6" />
  </svg>
);

const TABS: { key: Tab; label: string; icon: ReactNode }[] = [
  { key: "tasks", label: "Задачи", icon: ICON_TASKS },
  { key: "me", label: "О себе", icon: ICON_ME },
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
          {tab.icon}
          <span>{tab.label}</span>
        </button>
      ))}
    </nav>
  );
}
