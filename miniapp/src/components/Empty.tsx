/**
 * Пустое состояние: объяснение, фразы-подсказки и действие, а не пустая
 * таблица. Заводить записи здесь нельзя — их ведёт бот, поэтому
 * единственная кнопка возвращает в чат. `note` — строка под кнопкой,
 * если экрану есть что пообещать заранее.
 */
export function Empty({
  title,
  text,
  hints,
  note,
  onClose,
}: {
  title: string;
  text: string;
  hints: string[];
  note?: string;
  onClose: () => void;
}) {
  return (
    <div className="empty">
      <span className="empty__mark" aria-hidden="true">
        <i />
      </span>
      <h2 className="empty__h">{title}</h2>
      <p className="empty__p">{text}</p>
      <ul className="hints">
        {hints.map((hint) => (
          <li className="hint" key={hint}>
            {hint}
          </li>
        ))}
      </ul>
      <button type="button" className="btn btn--wide" onClick={onClose}>
        Вернуться в чат
      </button>
      {note ? <p className="note empty__note">{note}</p> : null}
    </div>
  );
}
