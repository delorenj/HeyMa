/**
 * Main dashboard component
 * Shows recording status, last transcription, and quick controls
 */

import { Box, Container, Grid } from '@mui/material';
import { useNavigate } from 'react-router-dom';
import StatusPanel from './StatusPanel';
import TranscriptionPanel from './TranscriptionPanel';
import RecordingControls from './RecordingControls';
import QuickActions from './QuickActions';
import ProfileSelector from '@components/Profile/ProfileSelector';
import Header from './Header';

export default function Dashboard() {
  const navigate = useNavigate();

  return (
    <Box sx={{ minHeight: '100vh', bgcolor: 'background.default' }}>
      <Header onSettingsClick={() => navigate('/settings')} />

      <Container maxWidth="lg" sx={{ py: 4 }}>
        <Grid container spacing={3}>
          {/* Profile Selector */}
          <Grid item xs={12}>
            <ProfileSelector />
          </Grid>

          {/* Recording Controls */}
          <Grid item xs={12}>
            <RecordingControls />
          </Grid>

          {/* Status Panel */}
          <Grid item xs={12} md={6}>
            <StatusPanel />
          </Grid>

          {/* Last Transcription */}
          <Grid item xs={12} md={6}>
            <TranscriptionPanel />
          </Grid>

          {/* Quick Actions */}
          <Grid item xs={12}>
            <QuickActions />
          </Grid>
        </Grid>
      </Container>
    </Box>
  );
}
