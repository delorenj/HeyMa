/**
 * Tests for RecordingControls component
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import RecordingControls from '@components/Dashboard/RecordingControls';
import { useRecordingControls } from '@hooks/useTauriState';
import { RecordingState } from '@types';

// Mock hooks
vi.mock('@hooks/useTauriState');

describe('RecordingControls', () => {
  const mockStartRecording = vi.fn();
  const mockStopRecording = vi.fn();
  const mockPauseRecording = vi.fn();
  const mockResumeRecording = vi.fn();

  beforeEach(() => {
    vi.clearAllMocks();

    vi.mocked(useRecordingControls).mockReturnValue({
      recording: RecordingState.Idle,
      startRecording: mockStartRecording,
      stopRecording: mockStopRecording,
      pauseRecording: mockPauseRecording,
      resumeRecording: mockResumeRecording,
      isRecording: false,
    });
  });

  it('should render start button when idle', () => {
    render(<RecordingControls />);

    const startButton = screen.getByRole('button', { name: /start/i });
    expect(startButton).toBeInTheDocument();
  });

  it('should call startRecording when start button clicked', async () => {
    const user = userEvent.setup();
    mockStartRecording.mockResolvedValue(undefined);

    render(<RecordingControls />);

    const startButton = screen.getByRole('button', { name: /start/i });
    await user.click(startButton);

    expect(mockStartRecording).toHaveBeenCalledTimes(1);
  });

  it('should render stop button when recording', () => {
    vi.mocked(useRecordingControls).mockReturnValue({
      recording: RecordingState.Listening,
      startRecording: mockStartRecording,
      stopRecording: mockStopRecording,
      pauseRecording: mockPauseRecording,
      resumeRecording: mockResumeRecording,
      isRecording: true,
    });

    render(<RecordingControls />);

    const stopButton = screen.getByRole('button', { name: /stop/i });
    expect(stopButton).toBeInTheDocument();
  });

  it('should call stopRecording when stop button clicked', async () => {
    const user = userEvent.setup();
    mockStopRecording.mockResolvedValue(undefined);

    vi.mocked(useRecordingControls).mockReturnValue({
      recording: RecordingState.Listening,
      startRecording: mockStartRecording,
      stopRecording: mockStopRecording,
      pauseRecording: mockPauseRecording,
      resumeRecording: mockResumeRecording,
      isRecording: true,
    });

    render(<RecordingControls />);

    const stopButton = screen.getByRole('button', { name: /stop/i });
    await user.click(stopButton);

    expect(mockStopRecording).toHaveBeenCalledTimes(1);
  });

  it('should show processing state', () => {
    vi.mocked(useRecordingControls).mockReturnValue({
      recording: RecordingState.Processing,
      startRecording: mockStartRecording,
      stopRecording: mockStopRecording,
      pauseRecording: mockPauseRecording,
      resumeRecording: mockResumeRecording,
      isRecording: true,
    });

    render(<RecordingControls />);

    expect(screen.getByText(/processing/i)).toBeInTheDocument();
  });

  it('should show error state', () => {
    vi.mocked(useRecordingControls).mockReturnValue({
      recording: RecordingState.Error,
      startRecording: mockStartRecording,
      stopRecording: mockStopRecording,
      pauseRecording: mockPauseRecording,
      resumeRecording: mockResumeRecording,
      isRecording: false,
    });

    render(<RecordingControls />);

    expect(screen.getByText(/error/i)).toBeInTheDocument();
  });

  it('should disable button during operation', async () => {
    const user = userEvent.setup();
    mockStartRecording.mockImplementation(() => new Promise(resolve => setTimeout(resolve, 100)));

    render(<RecordingControls />);

    const startButton = screen.getByRole('button', { name: /start/i });
    await user.click(startButton);

    // Button should be disabled during operation
    expect(startButton).toBeDisabled();
  });

  it('should handle recording error gracefully', async () => {
    const user = userEvent.setup();
    mockStartRecording.mockRejectedValue(new Error('Microphone not found'));

    render(<RecordingControls />);

    const startButton = screen.getByRole('button', { name: /start/i });
    await user.click(startButton);

    await waitFor(() => {
      expect(screen.getByText(/error/i)).toBeInTheDocument();
    });
  });

  it('should support keyboard shortcuts', async () => {
    const user = userEvent.setup();
    mockStartRecording.mockResolvedValue(undefined);

    render(<RecordingControls />);

    // Simulate keyboard shortcut (Ctrl+Shift+R)
    await user.keyboard('{Control>}{Shift>}r{/Shift}{/Control}');

    await waitFor(() => {
      expect(mockStartRecording).toHaveBeenCalled();
    });
  });
});
