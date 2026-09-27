import type { ReactNode } from "react";

/** Строка карточки «название — значение»; `note` — подпись под значением. */
export function Field({
  label,
  note,
  children,
}: {
  label: string;
  note?: string;
  children: ReactNode;
}) {
  return (
    <div className="kv__row">
      <span className="kv__k">{label}</span>
      <span className="kv__v">
        {children}
        {note ? <small>{note}</small> : null}
      </span>
    </div>
  );
}
