import { useId } from "react";

/**
 * Выбор из вариантов — ряд кнопок на обычных `radio`: подписью служит
 * сама кнопка, выбранная — цветом действия (`design.md` §3). Клавиатура
 * и экранный диктор работают как у любой группы радиокнопок.
 */
export function Choice<T extends string>({
  label,
  options,
  value,
  disabled = false,
  onChange,
}: {
  label: string;
  options: readonly (readonly [T, string])[];
  value: T;
  disabled?: boolean;
  onChange: (value: T) => void;
}) {
  const name = useId();
  return (
    <div className="seg" role="radiogroup" aria-label={label}>
      {options.map(([key, text]) => (
        <label className="seg__opt" key={key}>
          <input
            type="radio"
            name={name}
            value={key}
            checked={key === value}
            disabled={disabled}
            onChange={() => onChange(key)}
          />
          <span>{text}</span>
        </label>
      ))}
    </div>
  );
}
