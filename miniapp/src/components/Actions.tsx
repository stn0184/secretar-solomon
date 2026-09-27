export type ActionKind = "done" | "confirm" | "delete";

/** Главное действие рядом с «Удалить»: «Сделано» у задачи, «Подтвердить» у предположения. */
export interface PrimaryAction {
  kind: "done" | "confirm";
  label: string;
  busyLabel: string;
  onClick: () => void;
}

/**
 * Кнопки действий: главное (если есть) и «Удалить». Пока действие идёт,
 * обе заперты; отказ базы — текстом под кнопками, запись остаётся на
 * месте (инвариант 4).
 *
 * На карточке задачи блок прилипает к низу экрана; `inline` — внутри
 * раскрытой записи памяти, кнопки меньше и стоят по месту.
 */
export function Actions({
  busy,
  error,
  primary,
  onDelete,
  inline = false,
}: {
  busy: ActionKind | null;
  error: string | null;
  primary?: PrimaryAction;
  onDelete: () => void;
  inline?: boolean;
}) {
  const button = inline ? "btn btn--sm" : "btn";
  return (
    <div className={inline ? "actions actions--inline" : "actions"}>
      <div className="actions__row">
        {primary ? (
          <button type="button" className={button} disabled={busy !== null} onClick={primary.onClick}>
            {busy === primary.kind ? primary.busyLabel : primary.label}
          </button>
        ) : null}
        <button
          type="button"
          className={`${button} btn--danger`}
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
