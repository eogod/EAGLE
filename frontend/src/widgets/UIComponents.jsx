const iconBtnBase =
  'flex h-7 w-7 items-center justify-center rounded-md border border-subtle bg-transparent text-muted transition-colors hover:border-default hover:bg-card-hover hover:text-primary disabled:opacity-40'

export const IconButton = ({ className = '', hover: _hover = 'accent', ...props }) => (
  <button type="button" {...props} className={`${iconBtnBase} ${className}`} />
)

const dashedBtnBase =
  'flex h-7 w-7 items-center justify-center rounded-md border border-dashed border-subtle bg-transparent text-muted transition-colors hover:border-solid hover:border-default hover:bg-card-hover hover:text-primary'

export const DashedIconButton = ({ className = '', ...props }) => (
  <button type="button" {...props} className={`${dashedBtnBase} ${className}`} />
)

export const DropdownItem = ({ onClick, children, className = '' }) => (
  <button
    type="button"
    onClick={onClick}
    className={`flex w-full items-center justify-between gap-2 border-b border-subtle/60 px-3 py-2 text-left transition-colors last:border-b-0 hover:bg-card-hover ${className}`}
  >
    {children}
  </button>
)

const inputBase =
  'h-8 rounded-md border border-subtle bg-input px-2 text-xs text-primary outline-none transition-colors placeholder:text-muted focus:border-accent'

export const Input = ({ as, className = '', ...props }) => {
  if (as === 'select') {
    return <select {...props} className={`${inputBase} ${className}`} />
  }
  return <input {...props} className={`${inputBase} ${className}`} />
}

export const TextInput = ({ className = '', ...props }) => (
  <input
    {...props}
    className={`mb-2 h-8 w-full rounded-md border border-subtle bg-input px-2 text-xs text-primary outline-none transition-colors placeholder:text-muted focus:border-accent ${className}`}
  />
)

export const TransparentInput = ({ className = '', ...props }) => (
  <input
    {...props}
    className={`mb-2 w-full rounded-md border border-transparent bg-transparent px-2 py-1.5 text-[13px] font-medium text-primary outline-none transition-colors placeholder:text-muted hover:border-subtle focus:border-accent focus:bg-input ${className}`}
  />
)

const textareaBase =
  'max-h-[300px] min-h-[64px] w-full resize-none rounded-md border border-subtle bg-input p-2.5 font-mono text-[12px] leading-relaxed text-primary outline-none transition-colors placeholder:text-muted focus:border-accent'

export const Textarea = ({ className = '', ...props }) => (
  <textarea {...props} className={`${textareaBase} ${className}`} />
)

export const ReadonlyTextarea = ({ className = '', ...props }) => (
  <textarea {...props} className={`${textareaBase} ${className}`} />
)

const actionBtnBase =
  'rounded-md px-2 py-1 text-xs text-muted transition-colors hover:bg-card-hover hover:text-primary'

export const ActionButton = ({ hover = 'primary', className = '', ...props }) => {
  const hoverClass = {
    primary: 'hover:text-primary',
    danger: 'hover:text-danger',
    accent: 'hover:text-accent',
  }[hover]

  return (
    <button type="button" {...props} className={`${actionBtnBase} ${hoverClass} ${className}`} />
  )
}

export const RunButton = ({ className = '', ...props }) => (
  <button
    type="button"
    {...props}
    className={`flex items-center gap-1.5 whitespace-nowrap rounded-md px-2 py-1 text-xs text-secondary transition-colors hover:bg-card-hover hover:text-accent disabled:text-warning ${className}`}
  />
)
