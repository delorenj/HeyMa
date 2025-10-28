/**
 * Transcription panel component
 * Shows last transcription and recent history
 */

import { Paper, Box, Typography, List, ListItem, ListItemText } from '@mui/material';
import { useAppStore } from '@hooks/useTauriState';

export default function TranscriptionPanel() {
  const { lastTranscription, transcriptions } = useAppStore();

  return (
    <Paper sx={{ p: 3, height: '100%' }}>
      <Typography variant="h6" gutterBottom>
        Last Transcription
      </Typography>

      {lastTranscription ? (
        <Box>
          <Box
            sx={{
              p: 2,
              bgcolor: 'action.hover',
              borderRadius: 1,
              mb: 2,
            }}
          >
            <Typography variant="body1" sx={{ mb: 1 }}>
              {lastTranscription.text}
            </Typography>
            <Typography variant="caption" color="text.secondary">
              {new Date(lastTranscription.timestamp).toLocaleTimeString()} •
              Confidence: {Math.round(lastTranscription.confidence * 100)}%
            </Typography>
          </Box>

          {lastTranscription.response && (
            <Box
              sx={{
                p: 2,
                bgcolor: 'primary.main',
                color: 'primary.contrastText',
                borderRadius: 1,
                mb: 2,
              }}
            >
              <Typography variant="body2" sx={{ mb: 0.5, opacity: 0.8 }}>
                Response:
              </Typography>
              <Typography variant="body1">{lastTranscription.response}</Typography>
            </Box>
          )}

          {transcriptions.length > 1 && (
            <Box>
              <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
                Recent History
              </Typography>
              <List dense>
                {transcriptions.slice(1, 4).map((t) => (
                  <ListItem key={t.id} sx={{ px: 0 }}>
                    <ListItemText
                      primary={t.text}
                      secondary={new Date(t.timestamp).toLocaleTimeString()}
                      primaryTypographyProps={{
                        variant: 'body2',
                        noWrap: true,
                      }}
                      secondaryTypographyProps={{
                        variant: 'caption',
                      }}
                    />
                  </ListItem>
                ))}
              </List>
            </Box>
          )}
        </Box>
      ) : (
        <Box
          sx={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            height: 200,
          }}
        >
          <Typography variant="body2" color="text.secondary">
            No transcriptions yet
          </Typography>
        </Box>
      )}
    </Paper>
  );
}
