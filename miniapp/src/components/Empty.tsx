/**
 * Пустое состояние списка: объяснение и действие, а не пустая таблица.
 * Заводить задачи здесь нельзя — их ведёт бот, поэтому единственная кнопка
 * возвращает в чат.
 */
export function Empty({ onClose }: { onClose: () => void }) {
  return (
    <div className="empty">
      <span className="empty__mark" aria-hidden="true">
        <i />
      </span>
      <h2 className="empty__h">Задач пока нет</h2>
      <p className="empty__p">
        Напишите боту — и она появится здесь. Срок и напоминание он разберёт сам.
      </p>
      <ul className="hints">
        <li className="hint">«В пятницу отправить расчёт Кузнецову»</li>
        <li className="hint">«Я обещал Сергею перезвонить во вторник»</li>
      </ul>
      <button type="button" className="btn btn--wide" onClick={onClose}>
        Вернуться в чат
      </button>
    </div>
  );
}
