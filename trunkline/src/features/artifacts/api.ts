import type { CallToolResult, Tool } from '@modelcontextprotocol/client'
import type { McpUiResourceCsp } from '@modelcontextprotocol/ext-apps/app-bridge'
import { apiFetch, refuse } from '@/lib/api/auth'

export interface ArtifactView {
  artifact: {
    id: string
    kind: 'mcp_app'
    title: string
    invocation: {
      tool: Tool
      arguments: Record<string, unknown>
      result: CallToolResult
    }
  }
  resource: { html: string; ui: { csp?: McpUiResourceCsp | null } }
  sandbox_origin: string
  sandbox_url: string
  viewer_path: string
}

export async function fetchArtifact(url: string, signal: AbortSignal): Promise<ArtifactView> {
  const response = await apiFetch(url, { signal, cache: 'no-store' })
  if (!response.ok) return refuse(response)
  return response.json() as Promise<ArtifactView>
}

export async function fetchArtifactConfig(): Promise<{ path: string }> {
  const response = await apiFetch('/api/artifacts/config')
  if (!response.ok) return refuse(response)
  return response.json() as Promise<{ path: string }>
}
