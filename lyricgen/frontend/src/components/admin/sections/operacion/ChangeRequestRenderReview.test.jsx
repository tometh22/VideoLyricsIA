import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import ChangeRequestRenderReview from './ChangeRequestRenderReview';

afterEach(cleanup);
it('shows the request and complete saved lyrics before requiring explicit render approval', () => {
  const confirm = vi.fn();
  render(<ChangeRequestRenderReview review={{ comment: 'Usar la frase completa', segments: [
    { start: 144.26, text: 'Respirarse, emborrachar, morir y seguir viviendo' },
  ] }} onConfirm={confirm} onClose={vi.fn()} />);
  expect(screen.getByRole('dialog')).toHaveTextContent('Usar la frase completa');
  expect(screen.getByRole('list')).toHaveTextContent('2:24Respirarse, emborrachar, morir y seguir viviendo');
  const button = screen.getByRole('button', { name: 'Aprobar y re-renderizar' });
  expect(button).toBeDisabled();
  fireEvent.click(screen.getByRole('checkbox'));
  fireEvent.click(button);
  expect(confirm).toHaveBeenCalledOnce();
});
