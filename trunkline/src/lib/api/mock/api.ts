/**
 * Mock adapter — serves the design dataset with a small latency so loading
 * states are exercised. Replace call-by-call as real endpoints appear.
 */
import type {
  ChannelMeta,
  ConfigureModel,
  ControlData,
  ProjectInfo,
  StatusData,
  SurfaceInfo,
  ThreadDetail,
  ThreadSummary,
} from '../types'
import {
  channels,
  controlData,
  defaultThreadDetail,
  projectList,
  statusData,
  surfaces,
  threadDetails,
  threads,
} from './data'

const delay = (ms: number) => new Promise<void>((r) => setTimeout(r, ms))

export const defaultComposerText =
  'Relay this thread to slack/#eng-platform and keep the session attached.'

export const mockApi = {
  async listChannels(): Promise<ChannelMeta[]> {
    await delay(60)
    return channels
  },
  async listThreads(): Promise<Record<string, ThreadSummary[]>> {
    await delay(80)
    return threads
  },
  async getThreadDetail(id: string): Promise<ThreadDetail> {
    await delay(120)
    // Deep-copy: the console mutates its working copy (feelers, doc revs).
    return structuredClone(threadDetails[id] ?? defaultThreadDetail)
  },
  async listProjects(): Promise<ProjectInfo[]> {
    await delay(60)
    return projectList
  },
  async getSurfaces(): Promise<SurfaceInfo[]> {
    await delay(40)
    return surfaces
  },
  async getControlData(): Promise<ControlData> {
    await delay(100)
    return controlData
  },
  async getStatus(): Promise<StatusData> {
    await delay(40)
    return statusData
  },
  mockModels(): ConfigureModel[] {
    return [
      { id: 'inkling:deepseek:deepseek-v4-pro', name: 'inkling · v4-pro' },
      { id: 'claude:anthropic:opus-4.1', name: 'claude · opus-4.1' },
      { id: 'codex:openai:gpt-5.3', name: 'codex · gpt-5.3' },
    ]
  },
}
