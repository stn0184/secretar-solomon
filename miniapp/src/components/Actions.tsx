export type ActionKind = "done" | "delete";

/**
 * Две кнопки карточки. Пока действие идёт, обе заперты; отказ базы —
 * текстом под кнопками, задача остаётся на месте (инвариант 4).
 */
export function Actions({
  busy,
  error,
  onDone,
  onDelete,
}: {
  busy: ActionKind | null;
  error: string | null;
  onDone: () => void;
  onDelete: () => void;
}) {
  return (
    <div className="actions">
      <div className="actions__row">
        <button type="button" className="btn" disabled={busy !== null} onClick={onDone}>
          {busy === "done" ? "Закрываем…" : "Сделано"}
        </button>
        <button
          type="button"
          className="btn btn--danger"
          disabled={busy !== null}
          onClick={onDelete}
        >
          {busy === "delete" ? "Удаляем…" : "Удалить"}
        </button>
      </div>
      {error ? (
        <p className="actions__error" role="alert">
          {error}
        </p>
      ) : null}
    </div>
  );
}
