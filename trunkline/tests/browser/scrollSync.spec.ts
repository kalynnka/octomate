import { test, expect } from '@playwright/test'

for (const zoom of [0.9, 1.3]) {
  for (const source of ['chat', 'timeline'] as const) {
    test(`scroll sync follows between rows from ${source} at ${zoom} zoom`, async ({ page }) => {
      await page.goto('/tests/browser/markdown.html')
      await page.evaluate(async zoom => {
        const modulePath = '/src/state/console.ts'
        const { syncRails } = await import(modulePath)
        document.documentElement.style.zoom = String(zoom)
        const host = document.createElement('div')
        host.style.cssText = 'position:fixed;inset:0;display:flex;background:white'
        document.body.append(host)
        for (const source of ['chat', 'timeline'] as const) {
          const pane = document.createElement('div')
          pane.id = source === 'chat' ? 'trk-chatlog' : 'trk-timeline'
          pane.style.cssText = 'height:400px;width:300px;padding:0;overflow:auto;flex-shrink:0'
          const content = document.createElement('div')
          content.style.padding = '100px 0 500px'
          for (let index = 0; index < 20; index++) {
            const row = document.createElement('div')
            row.textContent = `Message ${index}`
            row.style.height = source === 'chat' ? '300px' : '80px'
            if (source === 'chat') row.id = `pm-row-${index}`
            else row.dataset.uid = `row-${index}`
            content.append(row)
          }
          pane.append(content)
          pane.addEventListener('scroll', () => syncRails(source))
          host.append(pane)
        }
      }, zoom)
      const motion = await page.evaluate(async source => {
        const from = document.getElementById(source === 'chat' ? 'trk-chatlog' : 'trk-timeline')!
        const to = document.getElementById(source === 'chat' ? 'trk-timeline' : 'trk-chatlog')!
        const fromHeight = source === 'chat' ? 300 : 80
        const toHeight = source === 'chat' ? 80 : 300
        from.scrollTop = 54 + 8 * fromHeight
        const start = performance.now()
        const samples: { elapsed: number; top: number; source: number }[] = []
        let retargeted = false
        await new Promise<void>(resolve => {
          const sample = (now: number) => {
            samples.push({ elapsed: now - start, top: to.scrollTop, source: from.scrollTop })
            if (!retargeted && now - start > 120) {
              retargeted = true
              from.scrollTop = 54 + 12 * fromHeight
            }
            if (now - start < 1000) requestAnimationFrame(sample)
            else resolve()
          }
          requestAnimationFrame(sample)
        })
        return { samples, first: 54 + 8 * toHeight, last: 54 + 12 * toHeight, source: 54 + 12 * fromHeight }
      }, source)
      expect(motion.samples.filter(sample => sample.top > 2 && sample.top < motion.first - 2).length).toBeGreaterThan(3)
      expect(motion.samples.filter((sample, i) => i && sample.top < motion.samples[i - 1]!.top - 1)).toHaveLength(0)
      expect(Math.abs(motion.samples.at(-1)!.top - motion.last)).toBeLessThan(2)
      expect(Math.abs(motion.samples.at(-1)!.source - motion.source)).toBeLessThan(2)

      // Interrupt an unfinished follow animation from the other pane. It must
      // yield to the reader instead of writing over their new position.
      const interrupted = await page.evaluate(async source => {
        const from = document.getElementById(source === 'chat' ? 'trk-chatlog' : 'trk-timeline')!
        const to = document.getElementById(source === 'chat' ? 'trk-timeline' : 'trk-chatlog')!
        const fromHeight = source === 'chat' ? 300 : 80
        const toHeight = source === 'chat' ? 80 : 300
        from.scrollTop = 54 + 3 * fromHeight
        await new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve())))
        to.scrollTop = 54 + 6 * toHeight
        await new Promise(resolve => setTimeout(resolve, 1000))
        return { source: from.scrollTop, target: to.scrollTop, expectedSource: 54 + 6 * fromHeight, expectedTarget: 54 + 6 * toHeight }
      }, source)
      expect(Math.abs(interrupted.source - interrupted.expectedSource)).toBeLessThan(2)
      expect(Math.abs(interrupted.target - interrupted.expectedTarget)).toBeLessThan(2)

      await page.emulateMedia({ reducedMotion: 'reduce' })
      const positions = await page.evaluate(async source => {
        const from = document.getElementById(source === 'chat' ? 'trk-chatlog' : 'trk-timeline')!
        const to = document.getElementById(source === 'chat' ? 'trk-timeline' : 'trk-chatlog')!
        const fromHeight = source === 'chat' ? 300 : 80
        const toHeight = source === 'chat' ? 80 : 300
        const samples: { actual: number; expected: number; source: number; requested: number }[] = []
        for (const progress of [0.2, 0.35, 0.5, 0.65, 0.8]) {
          const requested = 100 + (3 + progress) * fromHeight - 46
          from.scrollTop = requested
          await new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve())))
          samples.push({ actual: to.scrollTop, expected: 100 + (3 + progress) * toHeight - 46, source: from.scrollTop, requested })
        }
        return samples
      }, source)
      for (const position of positions) {
        expect(Math.abs(position.actual - position.expected)).toBeLessThan(2)
        expect(Math.abs(position.source - position.requested)).toBeLessThan(2)
      }

      // A reader can immediately take over the other pane; our own scroll event
      // must not take control back or suppress that input for a timer interval.
      const handoff = await page.evaluate(async source => {
        const from = document.getElementById(source === 'chat' ? 'trk-timeline' : 'trk-chatlog')!
        const to = document.getElementById(source === 'chat' ? 'trk-chatlog' : 'trk-timeline')!
        const fromHeight = source === 'chat' ? 80 : 300
        const toHeight = source === 'chat' ? 300 : 80
        const requested = 100 + 4.5 * fromHeight - 46
        from.scrollTop = requested
        await new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve())))
        return { actual: to.scrollTop, expected: 100 + 4.5 * toHeight - 46, source: from.scrollTop, requested }
      }, source)
      expect(Math.abs(handoff.actual - handoff.expected)).toBeLessThan(2)
      expect(Math.abs(handoff.source - handoff.requested)).toBeLessThan(2)

      const folded = await page.evaluate(async () => {
        const log = document.getElementById('trk-chatlog')!
        const rail = document.getElementById('trk-timeline')!
        const group = document.createElement('details')
        group.className = 'trk-trace-group'
        group.style.cssText = 'display:block;padding:0;margin:0;border:0'
        const summary = document.createElement('summary')
        summary.style.height = '40px'
        summary.textContent = 'Five tools'
        group.append(summary)
        document.getElementById('pm-row-5')!.before(group)
        for (let index = 5; index < 10; index++) group.append(document.getElementById(`pm-row-${index}`)!)
        rail.scrollTop = 100 + 7 * 80 - 46
        await new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve())))
        const groupTop = log.scrollTop
        log.scrollTop = 100 + 5 * 300 + 20 - 46
        await new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve())))
        const between = rail.scrollTop
        const groupProgress = log.scrollTop - 1554
        const hidden = document.createElement('div')
        hidden.dataset.folded = ''
        hidden.style.cssText = 'height:0;overflow:hidden'
        rail.querySelector('[data-uid="row-0"]')!.before(hidden)
        for (let index = 0; index < 5; index++) hidden.append(rail.querySelector(`[data-uid="row-${index}"]`)!)
        log.scrollTop = 100 + 2 * 300 - 46
        await new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve())))
        return { groupTop, between, groupProgress, hidden: rail.scrollTop }
      })
      expect(Math.abs(folded.groupTop - 1554)).toBeLessThan(2)
      expect(Math.abs(folded.between - (454 + folded.groupProgress * 10))).toBeLessThan(2)
      expect(Math.abs(folded.hidden - 54 * 654 / 1554)).toBeLessThan(2)

      const ends = await page.evaluate(async source => {
        const from = document.getElementById(source === 'chat' ? 'trk-chatlog' : 'trk-timeline')!
        const to = document.getElementById(source === 'chat' ? 'trk-timeline' : 'trk-chatlog')!
        from.scrollTop = 0
        await new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve())))
        const start = to.scrollTop
        from.scrollTop = from.scrollHeight
        await new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve())))
        return { start, remaining: to.scrollHeight - to.clientHeight - to.scrollTop }
      }, source)
      expect(ends.start).toBeLessThan(2)
      expect(ends.remaining).toBeLessThan(2)
    })
  }
}
