import type { ControlSection } from '@/state/console'

export const controlHints: Record<Exclude<ControlSection, ''>, string> = {
  dash: 'overview',
  agents: 'models',
  mcp: 'tools',
  profile: 'accounts',
  channels: 'accounts',
  keys: 'access',
  settings: 'appearance',
}
