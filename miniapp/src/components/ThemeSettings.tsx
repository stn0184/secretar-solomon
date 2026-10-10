import { useId } from "react";

import type { ColorScheme } from "../lib/telegram.ts";
import { THEME_CHOICES, type ThemeChoice, autoNote } from "../lib/theme.ts";
import { Header } from "./Header.tsx";

const TITLE: Record<ThemeChoice, string> = {
  auto: "Как в Telegram",
  quiet: "Тихая роскошь",
  bank: "Банк-люкс",
};

const NOTE: Record<Exclude<ThemeChoice, "auto">, string> = {
  quiet: "Светлая: бумага, воздух, антиква",
  bank: "Тёмная: чёрный, золото, сферы-камни",
};

/**
 * «Оформление» (`techspec/32-redesign.md` §32.1, артборд 06): три варианта с
 * превью — «Как в Telegram» (по умолчанию), «Тихая роскошь», «Банк-люкс».
 * Вход — меню ⋯ Telegram → «Настройки». Выбор сразу меняет вид, а
 * сохраняет его база (`save_theme`): отказ — текстом под вариантами, и
 * выбор возвращается к прежнему. Сохранённое открывается так же после
 * перезапуска и на любом устройстве.
 *
 * Варианты — обычные `radio`: клавиатура и экранный диктор работают как у
 * любой группы радиокнопок.
 */
export function ThemeSettings({
  choice,
  scheme,
  busy,
  error,
  onChoose,
  onBack,
}: {
  choice: ThemeChoice;
  scheme: ColorScheme;
  busy: boolean;
  error: string | null;
  onChoose: (choice: ThemeChoice) => void;
  onBack: () => void;
}) {
  const name = useId();
  return (
    <main className="screen">
      <Header eyebrow="Настройки" title="Оформление" onBack={onBack} />
      <div className="card opts" role="radiogroup" aria-label="Оформление">
        {THEME_CHOICES.map((option) => (
          <label className="opt" key={option}>
            <input
              type="radio"
              name={name}
              value={option}
              checked={option === choice}
              disabled={busy}
              onChange={() => onChoose(option)}
            />
            <span className={`pv pv--${option}`} aria-hidden="true">
              <i />
              <i />
              <i />
              <i />
            </span>
            <span className="opt__b">
              <span className="opt__t">{TITLE[option]}</span>
              <span className="opt__m">{option === "auto" ? autoNote(scheme) : NOTE[option]}</span>
            </span>
            <span className="radio" aria-hidden="true" />
          </label>
        ))}
      </div>
      {error ? (
        <p className="actions__error" role="alert">
          {error}
        </p>
      ) : null}
      <p className="note">
        «Как в Telegram» меняется само: тёмная тема Telegram — <b>Банк-люкс</b>, светлая —{" "}
        <b>Тихая роскошь</b>. Экраны те же, меняется только вид.
      </p>
      <section className="sec">
        <h2 className="sec__h">
          <span>Движение</span>
        </h2>
        <p className="note">
          Цифры прокручиваются, фон медленно плывёт. Если в телефоне включено «Уменьшить движение»,
          всё стоит на месте.
        </p>
      </section>
    </main>
  );
}
