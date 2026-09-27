export default {
  theme: {
    extend: {
      borderRadius: { nano: 'var(--radius-nano)', micro: 'var(--radius-micro)', macro: 'var(--radius-macro)', mega: 'var(--radius-mega)' },
      colors: {
        background: 'var(--background)', foreground: 'var(--foreground)',
        primary: { DEFAULT: 'var(--primary)', foreground: 'var(--primary-foreground)' },
        border: 'var(--border)', muted: 'var(--muted)', ink: 'var(--ink)',
      },
    },
  },
}
