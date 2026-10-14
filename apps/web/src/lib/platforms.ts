import type { PlatformId } from '../types/contract'

export const PLATFORM_LABELS: Record<string, string> = {
  xzzd: '学在浙大',
  zykt: '智云课堂',
  manual: '手动整理',
}

export function platformLabel(platform: PlatformId): string {
  return PLATFORM_LABELS[platform] ?? platform
}
