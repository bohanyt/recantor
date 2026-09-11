import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import type { TranscriptSegmentResponse } from '../api/generated/types.gen';
import { TranscriptLog } from './TranscriptLog';

function segment(sequence: number, text: string): TranscriptSegmentResponse {
  return {
    id: `00000000-0000-4000-8000-${String(sequence).padStart(12, '0')}`,
    sequence,
    start_ms: (sequence - 1) * 1_000,
    end_ms: sequence * 1_000,
    text,
    language: 'id',
    created_at: '2026-09-11T00:00:00Z',
  };
}

describe('TranscriptLog', () => {
  it('announces additions through a restrained log instead of a whole-list live region', () => {
    render(<TranscriptLog segments={[segment(1, 'pertama'), segment(2, 'kedua')]} />);

    const log = screen.getByRole('log', { name: 'Live transcript updates' });
    expect(log).toHaveAttribute('aria-relevant', 'additions');
    expect(log).toHaveAttribute('aria-atomic', 'false');
    expect(log).not.toHaveAttribute('aria-live');
    expect(screen.getAllByRole('article')).toHaveLength(2);
    expect(screen.getByText('pertama')).toBeInTheDocument();
    expect(screen.getByText('kedua')).toBeInTheDocument();
  });
});
