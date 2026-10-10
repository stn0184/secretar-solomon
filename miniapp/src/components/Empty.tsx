import type { ReactNode } from "react";

/**
 * Пустое состояние (`design.md` §2): заголовок, абзац зачем, две
 * фразы-примера пузырями, строка подсказки и кнопка, которая закрывает
 * приложение и возвращает в чат. Нулей, пустых колец и пустой сетки нет.
 * Заводить записи здесь нельзя — их ведёт бот, поэтому кнопка одна.
 *
 * `blank` — место под будущее содержимое над текстом (пунктир «Первая
 * сфера» на «Сферах»); `action` — слово на кнопке.
 */
export function Empty({
  title,
  text,
  examples,
  hint,
  action = "Вернуться в чат",
  blank,
  onClose,
}: {
  title: string;
  text: string;
  examples: readonly string[];
  hint?: string;
  action?: string;
  blank?: ReactNode;
  onClose: () => void;
}) {
  return (
    <div className="empty">
      {blank}
      <h2 className="empty__t">{title}</h2>
      <p className="empty__p">{text}</p>
      <ul className="empty__ex">
        {examples.map((example) => (
          <li key={example}>«{example}»</li>
        ))}
      </ul>
      {hint ? <p className="empty__hint">{hint}</p> : null}
      <button type="button" className="btn btn--main btn--wide cta" onClick={onClose}>
        {action}
      </button>
    </div>
  );
}
