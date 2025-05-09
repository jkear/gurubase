import logging
from django.conf import settings
import requests

from core.integrations.strategy import IntegrationStrategy
logger = logging.getLogger(__name__)


class DiscordStrategy(IntegrationStrategy):
    def _get_bot_token(self, integration=None) -> str:
        """Helper to get the appropriate bot token based on environment"""
        if settings.ENV == 'selfhosted':
            integration = integration or self.get_integration()
            return integration.access_token
        return settings.DISCORD_BOT_TOKEN

    def exchange_token(self, code: str) -> dict:
        token_url = 'https://discord.com/api/oauth2/token'
        data = {
            'client_id': settings.DISCORD_CLIENT_ID,
            'client_secret': settings.DISCORD_CLIENT_SECRET,
            'grant_type': 'authorization_code',
            'code': code,
            'redirect_uri': settings.DISCORD_REDIRECT_URI
        }
        response = requests.post(token_url, data=data)
        if not response.ok:
            logger.error(f"Discord API error: {response.text}")
            raise ValueError(f"Discord API error: {response.text}")
        return response.json()

    def get_external_id(self, token_response: dict) -> str:
        guild_id = token_response.get('guild', {}).get('id')
        if not guild_id:
            raise ValueError("No guild ID found in the OAuth response")
        return guild_id
    
    def get_workspace_name(self, token_response: dict) -> str:
        return token_response.get('guild', {}).get('name')

    def list_channels(self) -> list:
        def _list_channels() -> list:
            integration = self.get_integration()
            channels_response = requests.get(
                f'https://discord.com/api/guilds/{integration.external_id}/channels',
                headers={'Authorization': f"Bot {self._get_bot_token(integration)}"}
            )
            channels_response.raise_for_status()
            channels = channels_response.json()
            
            # Only include text channels
            text_channels = [
                {
                    'id': c['id'],
                    'name': c['name'],
                    'allowed': False,
                    'type': 'text' if c['type'] == 0 else 'forum' if c['type'] == 15 else 'unknown'
                }
                for c in channels
                if c['type'] in [0, 15]  # 0 is text channel, 15 is forum
            ]
            return sorted(text_channels, key=lambda x: x['name'])

        return self.handle_api_call(_list_channels)

    def get_type(self) -> str:
        return 'DISCORD'

    def send_test_message(self, channel_id: str) -> bool:
        def _send_test_message() -> bool:
            integration = self.get_integration()
            
            # Find the channel type from integration.channels
            channel_type = 'text'  # default to text
            for channel in integration.channels:
                if channel['id'] == channel_id:
                    channel_type = channel.get('type', 'text')
                    break

            url = f'https://discord.com/api/channels/{channel_id}'
            headers = {'Authorization': f'Bot {self._get_bot_token(integration)}'}
            
            if channel_type == 'forum':
                # Create a forum post
                url += '/threads'
                data = {
                    'name': 'Test Message from Gurubase',
                    'message': {
                        'content': '👋 Hello! This is a test message from your Guru. I am working correctly!'
                    }
                }
            else:
                # Regular text channel message
                url += '/messages'
                data = {
                    'content': '👋 Hello! This is a test message from your Guru. I am working correctly!'
                }
            
            response = requests.post(url, headers=headers, json=data)
            response.raise_for_status()
            return True

        try:
            return self.handle_api_call(_send_test_message)
        except Exception as e:
            logger.error(f"Error sending Discord test message: {e}", exc_info=True)
            return False

    def revoke_access_token(self) -> None:
        """Revoke Discord OAuth token."""
        def _revoke_token() -> None:
            integration = self.get_integration()
            token_url = 'https://discord.com/api/oauth2/token/revoke'
            headers = {
                'Content-Type': 'application/x-www-form-urlencoded'
            }
            data = {
                'client_id': settings.DISCORD_CLIENT_ID,
                'client_secret': settings.DISCORD_CLIENT_SECRET,
                'token': integration.access_token
            }
            response = requests.post(token_url, headers=headers, data=data)
            response.raise_for_status()

        return self.handle_api_call(_revoke_token)

    def refresh_access_token(self, refresh_token: str) -> dict:
        """Refresh Discord OAuth token."""
        try:
            token_url = 'https://discord.com/api/oauth2/token'
            data = {
                'client_id': settings.DISCORD_CLIENT_ID,
                'client_secret': settings.DISCORD_CLIENT_SECRET,
                'grant_type': 'refresh_token',
                'refresh_token': refresh_token
            }
            response = requests.post(token_url, data=data)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error(f"Error refreshing Discord token: {e}", exc_info=True)
            raise

    def fetch_workspace_details(self, bot_token: str) -> dict:
        """Fetch Discord guild details using bot token"""
        response = requests.get(
            'https://discord.com/api/v10/users/@me/guilds',
            headers={
                'Authorization': f'Bot {bot_token}',
                'Content-Type': 'application/json'
            }
        )
        response.raise_for_status()
        guilds = response.json()
        
        if not guilds:
            raise ValueError("No guilds found for the bot")
            
        # For selfhosted, we'll use the first guild the bot has access to
        guild = guilds[0]
        return {
            'external_id': guild['id'],
            'workspace_name': guild['name']
        }
