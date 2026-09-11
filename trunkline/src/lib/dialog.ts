export async function closeDialog(dialog: HTMLDialogElement | null, onClose: () => void) {
  if (!dialog?.open || dialog.dataset.closing) return
  dialog.dataset.closing = 'true'
  dialog.inert = true
  await Promise.allSettled(dialog.getAnimations().map((animation) => animation.finished))
  if (!dialog.isConnected || !dialog.open) return
  dialog.close()
  onClose()
}
