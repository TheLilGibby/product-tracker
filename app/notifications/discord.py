import requests
import json
from datetime import datetime

class DiscordNotifier:
    """Send notifications to Discord via webhooks"""
    
    @staticmethod
    def send_notification(webhook_url, product_name, product_url, current_price, old_price=None, 
                         is_availability_alert=False, is_auto_cart=False, cart_url=None, image_url=None):
        """
        Send a notification to Discord via webhook
        
        Args:
            webhook_url: Discord webhook URL
            product_name: Name of the product
            product_url: URL to the product
            current_price: Current price of the product
            old_price: Previous price (for price drop alerts)
            is_availability_alert: Whether this is an availability alert
            is_auto_cart: Whether this is an auto-cart notification
            cart_url: URL to the cart (for auto-cart notifications)
            image_url: URL to product image
            
        Returns:
            Boolean indicating success or failure
        """
        if not webhook_url:
            return False
            
        if is_auto_cart:
            title = "🛒 Auto-Cart Success!"
            color = 3447003  # Blue for auto-cart
        elif is_availability_alert:
            title = "🔔 Product Available!"
            color = 5814783  # Green for availability
        else:
            title = "🔔 Price Drop Alert!"
            color = 15158332  # Red for price drop
            
        embed = {
            "title": title,
            "description": f"**{product_name}**",
            "color": color,
            "timestamp": datetime.utcnow().isoformat(),
            "url": product_url,
            "fields": [
                {
                    "name": "Product Link",
                    "value": f"[Click here to view product]({product_url})",
                    "inline": False
                }
            ],
            "footer": {
                "text": "Product Tracker Bot"
            }
        }
        
        # Add thumbnail if image URL is available
        if image_url:
            embed["thumbnail"] = {"url": image_url}
        
        if is_auto_cart:
            # Add cart URL if available
            if cart_url:
                embed["fields"].append({
                    "name": "Cart Link",
                    "value": f"[Click here to view cart]({cart_url})",
                    "inline": False
                })
            
            # Add price information if available
            if current_price:
                embed["fields"].append({
                    "name": "Current Price",
                    "value": f"${current_price:.2f}",
                    "inline": True
                })
            
            embed["fields"].append({
                "name": "Status",
                "value": "Product has been automatically added to cart!",
                "inline": False
            })
        elif is_availability_alert:
            embed["fields"].append({
                "name": "Status",
                "value": "Product is now in stock!",
                "inline": True
            })
            if current_price:
                embed["fields"].append({
                    "name": "Current Price",
                    "value": f"${current_price:.2f}",
                    "inline": True
                })
        else:
            # Price drop alert
            embed["fields"].append({
                "name": "Current Price",
                "value": f"${current_price:.2f}",
                "inline": True
            })
            
            if old_price:
                embed["fields"].append({
                    "name": "Previous Price",
                    "value": f"${old_price:.2f}",
                    "inline": True
                })
                
                savings = old_price - current_price
                percent_savings = (savings / old_price) * 100
                
                embed["fields"].append({
                    "name": "You Save",
                    "value": f"${savings:.2f} ({percent_savings:.1f}%)",
                    "inline": True
                })
        
        data = {
            "embeds": [embed],
            "content": f"{'Product Available Alert' if is_availability_alert else 'Price Drop Alert'}: {product_name}"
        }
        
        try:
            response = requests.post(webhook_url, json=data)
            return response.status_code == 204  # Discord returns 204 on success
        except Exception as e:
            print(f"Error sending Discord notification: {e}")
            return False 