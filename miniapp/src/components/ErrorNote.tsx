/** Отказ текстом по-русски и одно действие — «Обновить». */
export function ErrorNote({ message, onRetry }: { message: string; onRetry: () => void }) {
  return (
    <div className="error" role="alert">
      <p className="error__text">{message}</p>
      <button type="button" className="btn btn--ghost" onClick={onRetry}>
        Обновить
      </button>
    </div>
  );
}
