/**
 * Reusable confirmation dialog component
 * Supports different severity levels and custom actions
 */

import {
  Dialog,
  DialogTitle,
  DialogContent,
  DialogContentText,
  DialogActions,
  Button,
  CircularProgress,
} from '@mui/material';
import WarningAmberIcon from '@mui/icons-material/WarningAmber';
import ErrorOutlineIcon from '@mui/icons-material/ErrorOutline';
import InfoOutlinedIcon from '@mui/icons-material/InfoOutlined';
import { useState } from 'react';

export type ConfirmDialogSeverity = 'info' | 'warning' | 'error';

export interface ConfirmDialogProps {
  open: boolean;
  title: string;
  message: string;
  severity?: ConfirmDialogSeverity;
  confirmText?: string;
  cancelText?: string;
  confirmColor?: 'primary' | 'secondary' | 'error' | 'warning' | 'info' | 'success';
  onConfirm: () => void | Promise<void>;
  onCancel: () => void;
  loading?: boolean;
}

function getSeverityIcon(severity: ConfirmDialogSeverity) {
  switch (severity) {
    case 'error':
      return <ErrorOutlineIcon color="error" sx={{ fontSize: 48 }} />;
    case 'warning':
      return <WarningAmberIcon color="warning" sx={{ fontSize: 48 }} />;
    case 'info':
    default:
      return <InfoOutlinedIcon color="info" sx={{ fontSize: 48 }} />;
  }
}

export default function ConfirmDialog({
  open,
  title,
  message,
  severity = 'info',
  confirmText = 'Confirm',
  cancelText = 'Cancel',
  confirmColor = 'primary',
  onConfirm,
  onCancel,
  loading: externalLoading,
}: ConfirmDialogProps) {
  const [internalLoading, setInternalLoading] = useState(false);
  const loading = externalLoading ?? internalLoading;

  const handleConfirm = async () => {
    try {
      setInternalLoading(true);
      await onConfirm();
    } finally {
      setInternalLoading(false);
    }
  };

  return (
    <Dialog
      open={open}
      onClose={loading ? undefined : onCancel}
      maxWidth="sm"
      fullWidth
      aria-labelledby="confirm-dialog-title"
      aria-describedby="confirm-dialog-description"
    >
      <DialogTitle
        id="confirm-dialog-title"
        sx={{
          display: 'flex',
          alignItems: 'center',
          gap: 2,
        }}
      >
        {getSeverityIcon(severity)}
        {title}
      </DialogTitle>

      <DialogContent>
        <DialogContentText id="confirm-dialog-description">{message}</DialogContentText>
      </DialogContent>

      <DialogActions sx={{ px: 3, pb: 2 }}>
        <Button onClick={onCancel} disabled={loading} color="inherit">
          {cancelText}
        </Button>
        <Button
          onClick={handleConfirm}
          disabled={loading}
          color={confirmColor}
          variant="contained"
          autoFocus
          startIcon={loading ? <CircularProgress size={20} /> : undefined}
        >
          {loading ? 'Processing...' : confirmText}
        </Button>
      </DialogActions>
    </Dialog>
  );
}

/**
 * Hook for managing confirm dialog state
 */
export function useConfirmDialog() {
  const [dialogState, setDialogState] = useState<{
    open: boolean;
    title: string;
    message: string;
    severity: ConfirmDialogSeverity;
    confirmText: string;
    cancelText: string;
    confirmColor: NonNullable<ConfirmDialogProps['confirmColor']>;
    onConfirm: () => void | Promise<void>;
  }>({
    open: false,
    title: '',
    message: '',
    severity: 'info',
    confirmText: 'Confirm',
    cancelText: 'Cancel',
    confirmColor: 'primary',
    onConfirm: () => {},
  });

  const showConfirm = (
    options: Omit<ConfirmDialogProps, 'open' | 'onCancel' | 'loading'>
  ): Promise<boolean> => {
    return new Promise((resolve) => {
      setDialogState({
        open: true,
        title: options.title,
        message: options.message,
        severity: options.severity ?? 'info',
        confirmText: options.confirmText ?? 'Confirm',
        cancelText: options.cancelText ?? 'Cancel',
        confirmColor: options.confirmColor ?? 'primary',
        onConfirm: async () => {
          await options.onConfirm();
          setDialogState((prev) => ({ ...prev, open: false }));
          resolve(true);
        },
      });
    });
  };

  const handleCancel = () => {
    setDialogState((prev) => ({ ...prev, open: false }));
  };

  const dialog = (
    <ConfirmDialog
      open={dialogState.open}
      title={dialogState.title}
      message={dialogState.message}
      severity={dialogState.severity}
      confirmText={dialogState.confirmText}
      cancelText={dialogState.cancelText}
      confirmColor={dialogState.confirmColor}
      onConfirm={dialogState.onConfirm}
      onCancel={handleCancel}
    />
  );

  return { showConfirm, dialog };
}
