from django.db import models


class MetaAdsConnection(models.Model):
    """Stores one Meta Ads account connection. One per deployment for now."""
    name = models.CharField(max_length=100, default="Nelson Hospital")
    page_access_token = models.TextField(help_text="Long-lived Page Access Token from Meta")
    ad_account_id = models.CharField(max_length=100, help_text="Ad Account ID (without act_ prefix)")
    page_id = models.CharField(max_length=100, help_text="Facebook Page ID")
    webhook_verify_token = models.CharField(max_length=100, help_text="Secret token for webhook verification")
    is_active = models.BooleanField(default=True)
    connected_at = models.DateTimeField(auto_now_add=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Meta Ads Connection"

    def __str__(self):
        return f"{self.name} — Meta Ads"


class MetaLeadForm(models.Model):
    """A specific Lead Ads form to sync from Meta."""
    connection = models.ForeignKey(MetaAdsConnection, on_delete=models.CASCADE, related_name="forms")
    form_id = models.CharField(max_length=100, unique=True)
    form_name = models.CharField(max_length=255)
    is_syncing = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.form_name


class MetaCampaignStat(models.Model):
    """Daily snapshot of campaign-level metrics from Meta Insights API."""
    campaign_id = models.CharField(max_length=100)
    campaign_name = models.CharField(max_length=255)
    date_preset = models.CharField(max_length=50, default="last_30d")
    spend = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    impressions = models.PositiveIntegerField(default=0)
    clicks = models.PositiveIntegerField(default=0)
    leads_count = models.PositiveIntegerField(default=0)
    reach = models.PositiveIntegerField(default=0)
    synced_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("campaign_id", "date_preset")
        ordering = ["-spend"]

    @property
    def cpl(self):
        return round(float(self.spend) / self.leads_count, 2) if self.leads_count else 0

    @property
    def cpc(self):
        return round(float(self.spend) / self.clicks, 2) if self.clicks else 0

    @property
    def ctr(self):
        return round((self.clicks / self.impressions) * 100, 2) if self.impressions else 0

    def __str__(self):
        return f"{self.campaign_name} ({self.date_preset})"
