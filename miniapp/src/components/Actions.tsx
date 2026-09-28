export type ActionKind = "done" | "confirm" | "save" | "delete";

/**
 * Главное действие: «Сделано» у задачи, «Подтвердить» у предположения,
 * «Сохранить» в форме правки.
 */
export interface PrimaryAction {
  kind: "done" | "confirm" | "save";
  label: string;
  busyLabel: string;
  onClick: () => void;
}

/** Вторая кнопка вместо «Удалить» — спокойная, без красного: «Отмена» в форме. */
export interface SecondaryAction {
  label: string;
  onClick: () => void;
}

/**
 * Кнопки действий: главное (если есть) и «Удалить» — или вместо него
 * спокойная `secondary`. Пока действие идёт, обе заперты; отказ базы —
 * текстом под кнопками, запись остаётся на месте (инвариант 4).
 *
 * На карточке задачи блок прилипает к низу экрана; `inline` — внутри
 * раскрытой записи памяти, кнопки меньше и стоят по месту.
 */
export function Actions({
  busy,
  error,
  primary,
  onDelete,
  secondary,
  inline = false,
}: {
  busy: ActionKind | null;
  error: string | null;
  primary?: PrimaryAction;
  onDelete?: () => void;
  secondary?: SecondaryAction;
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
        {secondary ? (
          <button
            type="button"
            className={`${button} btn--ghost`}
            disabled={busy !== null}
            onClick={secondary.onClick}
          >
            {secondary.label}
          </button>
        ) : null}
        {onDelete && !secondary ? (
          <button
            type="button"
            className={`${button} btn--danger`}
            disabled={busy !== null}
            onClick={onDelete}
          >
            {busy === "delete" ? "Удаляем…" : "Удалить"}
          </button>
        ) : null}
      </div>
      {error ? (
        <p className="actions__error" role="alert">
          {error}
        </p>
      ) : null}
    </div>
  );
}
