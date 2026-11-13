import * as Switch from "@radix-ui/react-switch";

interface ToggleProps {
  checked: boolean;
  onChange: (checked: boolean) => void;
  label: string;
  id?: string;
  disabled?: boolean;
  /** The change was sent and is not confirmed yet. */
  pending?: boolean;
}

export function Toggle({ checked, onChange, label, id, disabled, pending }: ToggleProps) {
  return (
    <Switch.Root
      id={id}
      className="toggle"
      checked={checked}
      onCheckedChange={onChange}
      disabled={disabled}
      aria-label={label}
      aria-busy={pending || undefined}
      data-pending={pending || undefined}
    >
      <Switch.Thumb className="toggle-thumb" />
    </Switch.Root>
  );
}
