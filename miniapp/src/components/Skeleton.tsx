/** Серые плашки на месте строк — загрузка без спиннера (`design.md` §2). */
export function Skeleton({ rows = 3, label }: { rows?: number; label: string }) {
  return (
    <div className="card skeleton" aria-busy="true" aria-label={label}>
      {Array.from({ length: rows }, (_, index) => (
        <div key={index} className="skeleton__row" />
      ))}
    </div>
  );
}
