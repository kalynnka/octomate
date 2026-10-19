import type { CallToolResult, Tool } from '@modelcontextprotocol/client'
import type { McpUiResourceCsp } from '@modelcontextprotocol/ext-apps/app-bridge'
import { apiFetch, refuse } from '@/lib/api/auth'

export type ArtifactView = {
  sandbox_origin: string
  sandbox_url: string
  viewer_path: string
} & ({
  artifact: { id: string; title: string } & {
    kind: 'mcp_app'
    invocation: {
      tool: Tool
      arguments: Record<string, unknown>
      result: CallToolResult
    }
  }
  resource: { html: string; ui: { csp?: McpUiResourceCsp | null } }
} | {
  artifact: { id: string; title: string; kind: 'html'; file_id: string }
  html: string
})

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
