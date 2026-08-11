#!/usr/bin/env python3
"""
Third-party tool detection — the fingerprint table.

WHAT THIS DOES
--------------
A CRM never tells you "Gong is connected." It tells you there is a managed
package with the namespace `gong`, a connected app called "Gong for
Salesforce", an integration user `gong-integration@acme.com`, and 41 custom
fields whose API names start with `gong__`. Four weak signals; one obvious
conclusion. This module turns those signals into a named, clustered stack map.

HOW MATCHING WORKS (deliberately conservative)
----------------------------------------------
Four signal types, in descending order of how much we trust them:

  1. `sf_namespaces`      Salesforce managed-package namespace prefixes.
                          Matched EXACTLY (case-insensitive) against the
                          namespace on InstalledSubscriberPackage,
                          EntityDefinition and FieldDefinition. A namespace is
                          globally unique and registered with Salesforce, so an
                          exact hit is the strongest evidence available.
  2. `hs_prefixes`        HubSpot property-name prefixes. Matched as a prefix
                          (case-insensitive) against property API names. Strong,
                          but integrations sometimes create un-prefixed
                          properties, so absence proves nothing.
  3. `app_patterns`       Substrings matched against connected-app names, OAuth
                          app names and package publisher names. Human-entered,
                          so noisier.
  4. `user_patterns`      Substrings matched against integration-user names,
                          usernames and email local-parts. Noisiest — an admin
                          named "Clay Sanderson" should not summon Clay.

Substring patterns shorter than MIN_SUBSTRING characters are only honoured when
the entry sets "exact_app": true / "exact_user": true, which forces a
whole-token match instead. This is the single guard that keeps short tokens
("zi", "pi", "db") from lighting up half the table.

Every detection carries the signals that produced it, so the report can show
its work: "Gong — matched on namespace `gong`, connected app 'Gong for
Salesforce', 41 fields". A finding a customer cannot verify is not shippable.

CONFIDENCE
----------
`confidence` describes how sure we are of the SIGNATURE, not of the detection.
"high"   verified, distinctive, and stable across versions.
"medium" widely reported but the vendor has shipped more than one namespace or
         renamed the product (Chorus, Groove, Marketo Measure).
"low"    plausible but unverified; treat a lone low-confidence hit as a lead,
         not a fact. The report labels these.

EXTENDING THIS TABLE
--------------------
Add a dict to TOOLS. Nothing else needs to change — clusters, scoring and the
report all read from it. Two rules:
  · Never delete the "unidentified namespace" path. Any namespace seen in the
    org that matches nothing here is still reported, with the package name from
    InstalledSubscriberPackage. We would rather say "we don't know what `abc__`
    is" than silently drop it.
  · When you add a namespace you have actually confirmed in a live org, set
    confidence "high" and put the org's package name in "note".

Standard library only. No network. Safe to import from anywhere.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Sequence

# Substrings shorter than this are ignored unless the entry demands an exact
# token match. Four characters is where false positives fall off a cliff.
MIN_SUBSTRING = 4

# Cluster label -> human heading used by the stack map in the report.
CLUSTERS: Dict[str, str] = {
    "conversation_intelligence": "Conversation intelligence",
    "sales_engagement": "Sales engagement",
    "data_enrichment": "Data and enrichment",
    "revenue_intelligence": "Forecasting and revenue intelligence",
    "routing_scheduling": "Routing and scheduling",
    "marketing_automation": "Marketing automation",
    "attribution": "Attribution",
    "abm_intent": "ABM, intent and web conversion",
    "cpq_billing": "CPQ, billing and payments",
    "contracts_esign": "Contracts and e-signature",
    "customer_success": "Customer success",
    "support": "Support and ticketing",
    "product_analytics": "Product analytics",
    "ipaas_etl": "iPaaS, ETL and reverse ETL",
    "warehouse_bi": "Warehouse and BI",
    "comp_planning": "Commissions and comp planning",
    "enablement": "Enablement and content",
    "comms": "Communications",
    "finance_erp": "Finance and ERP",
    "ops_misc": "Other GTM ops",
}

# ---------------------------------------------------------------------------
# THE TABLE
#
# tool            display name
# category        key from CLUSTERS
# sf_namespaces   Salesforce managed-package namespace prefixes (exact match)
# hs_prefixes     HubSpot property-name prefixes (prefix match)
# app_patterns    substrings for connected apps / OAuth apps / package publishers
# user_patterns   substrings for integration user name / username / email
# exact_app       require a whole-token match for app_patterns (short tokens)
# exact_user      require a whole-token match for user_patterns (short tokens)
# confidence      high | medium | low  — confidence in the SIGNATURE
# note            anything a human reading the table needs to know
# ---------------------------------------------------------------------------

TOOLS: List[Dict[str, Any]] = [
    # ---------------------------------------------------- conversation intel
    {"tool": "Gong", "category": "conversation_intelligence",
     "sf_namespaces": ["gong"], "hs_prefixes": ["gong_"],
     "app_patterns": ["gong"], "user_patterns": ["gong"],
     "exact_app": True, "exact_user": True, "confidence": "high",
     "note": "Package usually appears as 'Gong for Salesforce'."},
    {"tool": "Chorus", "category": "conversation_intelligence",
     "sf_namespaces": ["chorusai", "chorus"], "hs_prefixes": ["chorus_"],
     "app_patterns": ["chorus"], "user_patterns": ["chorus"],
     "confidence": "medium",
     "note": "Now ZoomInfo Chorus; older orgs may still show the standalone package."},
    {"tool": "Clari Copilot", "category": "conversation_intelligence",
     "sf_namespaces": ["wingman", "claricopilot"], "hs_prefixes": ["wingman_"],
     "app_patterns": ["wingman", "clari copilot"], "user_patterns": ["wingman"],
     "confidence": "low", "note": "Formerly Wingman. Namespace unverified across versions."},
    {"tool": "Avoma", "category": "conversation_intelligence",
     "sf_namespaces": ["avoma"], "hs_prefixes": ["avoma_"],
     "app_patterns": ["avoma"], "user_patterns": ["avoma"], "confidence": "medium"},
    {"tool": "Fireflies", "category": "conversation_intelligence",
     "sf_namespaces": ["fireflies"], "hs_prefixes": ["fireflies_"],
     "app_patterns": ["fireflies"], "user_patterns": ["fireflies"], "confidence": "medium"},
    {"tool": "Fathom", "category": "conversation_intelligence",
     "sf_namespaces": [], "hs_prefixes": ["fathom_"],
     "app_patterns": ["fathom"], "user_patterns": ["fathom"], "confidence": "low"},
    {"tool": "Grain", "category": "conversation_intelligence",
     "sf_namespaces": [], "hs_prefixes": ["grain_"],
     "app_patterns": ["grain"], "user_patterns": ["grain"],
     "exact_app": True, "exact_user": True, "confidence": "low"},

    # ------------------------------------------------------ sales engagement
    {"tool": "Outreach", "category": "sales_engagement",
     "sf_namespaces": ["outreach"], "hs_prefixes": ["outreach_"],
     "app_patterns": ["outreach"], "user_patterns": ["outreach"], "confidence": "high"},
    {"tool": "Salesloft", "category": "sales_engagement",
     "sf_namespaces": ["salesloft"], "hs_prefixes": ["salesloft_"],
     "app_patterns": ["salesloft", "sales loft"], "user_patterns": ["salesloft"],
     "confidence": "high"},
    {"tool": "Apollo", "category": "sales_engagement",
     "sf_namespaces": ["apolloio", "apollo"], "hs_prefixes": ["apollo_"],
     "app_patterns": ["apollo"], "user_patterns": ["apollo"], "confidence": "medium",
     "note": "Apollo spans engagement and enrichment; filed under engagement."},
    {"tool": "Groove", "category": "sales_engagement",
     "sf_namespaces": ["groove"], "hs_prefixes": [],
     "app_patterns": ["groove"], "user_patterns": ["groove"],
     "exact_app": True, "exact_user": True, "confidence": "medium",
     "note": "Acquired by Clari; may present as 'Groove by Clari'."},
    {"tool": "Mixmax", "category": "sales_engagement",
     "sf_namespaces": ["mixmax"], "hs_prefixes": ["mixmax_"],
     "app_patterns": ["mixmax"], "user_patterns": ["mixmax"], "confidence": "low"},

    # ------------------------------------------------------- data enrichment
    {"tool": "ZoomInfo", "category": "data_enrichment",
     "sf_namespaces": ["zoominfo", "zi"], "hs_prefixes": ["zi_", "zoominfo_"],
     "app_patterns": ["zoominfo", "discoverorg"], "user_patterns": ["zoominfo", "zi"],
     "exact_user": True, "confidence": "high",
     "note": "`zi` is the legacy ZoomInfo/DiscoverOrg namespace and is exact-matched."},
    {"tool": "Clearbit", "category": "data_enrichment",
     "sf_namespaces": ["clearbit"], "hs_prefixes": ["clearbit_"],
     "app_patterns": ["clearbit"], "user_patterns": ["clearbit"], "confidence": "medium",
     "note": "Now part of HubSpot Breeze Intelligence; legacy properties persist."},
    {"tool": "Cognism", "category": "data_enrichment",
     "sf_namespaces": ["cognism"], "hs_prefixes": ["cognism_"],
     "app_patterns": ["cognism"], "user_patterns": ["cognism"], "confidence": "medium"},
    {"tool": "Clay", "category": "data_enrichment",
     "sf_namespaces": [], "hs_prefixes": ["clay_"],
     "app_patterns": ["clay"], "user_patterns": ["clay"],
     "exact_app": True, "exact_user": True, "confidence": "low",
     "note": "Exact-match only. 'Clay' is also a person's name."},
    {"tool": "LeadIQ", "category": "data_enrichment",
     "sf_namespaces": ["leadiq"], "hs_prefixes": ["leadiq_"],
     "app_patterns": ["leadiq"], "user_patterns": ["leadiq"], "confidence": "low"},
    {"tool": "Lusha", "category": "data_enrichment",
     "sf_namespaces": ["lusha"], "hs_prefixes": ["lusha_"],
     "app_patterns": ["lusha"], "user_patterns": ["lusha"],
     "exact_app": True, "exact_user": True, "confidence": "low"},
    {"tool": "Dun & Bradstreet", "category": "data_enrichment",
     "sf_namespaces": ["dnb", "datacloud"], "hs_prefixes": ["dnb_"],
     "app_patterns": ["dun & bradstreet", "dun and bradstreet", "d&b"],
     "user_patterns": ["dnb"], "exact_user": True, "confidence": "medium"},

    # -------------------------------------------------- revenue intelligence
    {"tool": "Clari", "category": "revenue_intelligence",
     "sf_namespaces": ["clari"], "hs_prefixes": ["clari_"],
     "app_patterns": ["clari"], "user_patterns": ["clari"],
     "exact_app": True, "exact_user": True, "confidence": "high"},
    {"tool": "BoostUp", "category": "revenue_intelligence",
     "sf_namespaces": ["boostup"], "hs_prefixes": [],
     "app_patterns": ["boostup"], "user_patterns": ["boostup"], "confidence": "low"},
    {"tool": "Aviso", "category": "revenue_intelligence",
     "sf_namespaces": ["aviso"], "hs_prefixes": [],
     "app_patterns": ["aviso"], "user_patterns": ["aviso"], "confidence": "low"},
    {"tool": "InsightSquared", "category": "revenue_intelligence",
     "sf_namespaces": ["is2", "insightsquared"], "hs_prefixes": [],
     "app_patterns": ["insightsquared", "mediafly"], "user_patterns": ["insightsquared"],
     "confidence": "low", "note": "Now Mediafly Intelligence360."},

    # -------------------------------------------------- routing & scheduling
    {"tool": "LeanData", "category": "routing_scheduling",
     "sf_namespaces": ["leandata"], "hs_prefixes": [],
     "app_patterns": ["leandata", "lean data"], "user_patterns": ["leandata"],
     "confidence": "high"},
    {"tool": "Chili Piper", "category": "routing_scheduling",
     "sf_namespaces": ["chilipiper"], "hs_prefixes": ["chilipiper_", "chili_"],
     "app_patterns": ["chili piper", "chilipiper"], "user_patterns": ["chilipiper", "chili"],
     "confidence": "high"},
    {"tool": "Calendly", "category": "routing_scheduling",
     "sf_namespaces": ["calendly"], "hs_prefixes": ["calendly_"],
     "app_patterns": ["calendly"], "user_patterns": ["calendly"], "confidence": "medium"},
    {"tool": "Traction Complete", "category": "routing_scheduling",
     "sf_namespaces": ["traction", "tcomplete"], "hs_prefixes": [],
     "app_patterns": ["traction complete"], "user_patterns": ["traction"],
     "confidence": "low"},
    {"tool": "RingLead", "category": "routing_scheduling",
     "sf_namespaces": ["ringlead"], "hs_prefixes": [],
     "app_patterns": ["ringlead"], "user_patterns": ["ringlead"], "confidence": "low",
     "note": "Now ZoomInfo OperationsOS."},
    {"tool": "Openprise", "category": "routing_scheduling",
     "sf_namespaces": ["openprise"], "hs_prefixes": [],
     "app_patterns": ["openprise"], "user_patterns": ["openprise"], "confidence": "low"},

    # ----------------------------------------------------- marketing automation
    {"tool": "Marketo", "category": "marketing_automation",
     "sf_namespaces": ["mkto_si", "mkto71"], "hs_prefixes": [],
     "app_patterns": ["marketo"], "user_patterns": ["marketo", "mkto"],
     "confidence": "high",
     "note": "`mkto_si` is the Marketo Sales Insight package; `mkto71` the sync package."},
    {"tool": "Pardot / Account Engagement", "category": "marketing_automation",
     "sf_namespaces": ["pi"], "hs_prefixes": [],
     "app_patterns": ["pardot", "account engagement"], "user_patterns": ["pardot"],
     "confidence": "high",
     "note": "`pi` namespace is exact-matched; it is only two characters."},
    {"tool": "HubSpot Marketing", "category": "marketing_automation",
     "sf_namespaces": ["hubspot", "hs"], "hs_prefixes": [],
     "app_patterns": ["hubspot"], "user_patterns": ["hubspot"], "confidence": "medium",
     "note": "Seen when HubSpot is the marketing layer on a Salesforce CRM-of-record."},
    {"tool": "Eloqua", "category": "marketing_automation",
     "sf_namespaces": ["eloqua"], "hs_prefixes": [],
     "app_patterns": ["eloqua", "oracle eloqua"], "user_patterns": ["eloqua"],
     "confidence": "medium"},
    {"tool": "Braze", "category": "marketing_automation",
     "sf_namespaces": ["braze"], "hs_prefixes": ["braze_"],
     "app_patterns": ["braze"], "user_patterns": ["braze"],
     "exact_app": True, "exact_user": True, "confidence": "low"},
    {"tool": "Customer.io", "category": "marketing_automation",
     "sf_namespaces": [], "hs_prefixes": ["customerio_"],
     "app_patterns": ["customer.io", "customerio"], "user_patterns": ["customerio"],
     "confidence": "low"},
    {"tool": "Iterable", "category": "marketing_automation",
     "sf_namespaces": ["iterable"], "hs_prefixes": ["iterable_"],
     "app_patterns": ["iterable"], "user_patterns": ["iterable"], "confidence": "low"},

    # ------------------------------------------------------------ attribution
    {"tool": "Marketo Measure (Bizible)", "category": "attribution",
     "sf_namespaces": ["bizible2", "bizible"], "hs_prefixes": ["bizible_"],
     "app_patterns": ["bizible", "marketo measure"], "user_patterns": ["bizible"],
     "confidence": "medium"},
    {"tool": "Dreamdata", "category": "attribution",
     "sf_namespaces": ["dreamdata"], "hs_prefixes": ["dreamdata_"],
     "app_patterns": ["dreamdata"], "user_patterns": ["dreamdata"], "confidence": "low"},
    {"tool": "HockeyStack", "category": "attribution",
     "sf_namespaces": [], "hs_prefixes": ["hockeystack_"],
     "app_patterns": ["hockeystack"], "user_patterns": ["hockeystack"], "confidence": "low"},

    # ------------------------------------------------------- ABM / intent / web
    {"tool": "6sense", "category": "abm_intent",
     "sf_namespaces": ["sixsense", "x6sense"], "hs_prefixes": ["sixsense_", "x6sense_"],
     "app_patterns": ["6sense", "sixsense"], "user_patterns": ["6sense", "sixsense"],
     "confidence": "low", "note": "Namespace varies by package generation — verify in-org."},
    {"tool": "Demandbase", "category": "abm_intent",
     "sf_namespaces": ["demandbase", "db"], "hs_prefixes": ["demandbase_"],
     "app_patterns": ["demandbase"], "user_patterns": ["demandbase"],
     "confidence": "low", "note": "`db` is exact-matched and is a known collision risk."},
    {"tool": "Terminus", "category": "abm_intent",
     "sf_namespaces": ["terminus"], "hs_prefixes": ["terminus_"],
     "app_patterns": ["terminus"], "user_patterns": ["terminus"], "confidence": "low"},
    {"tool": "Bombora", "category": "abm_intent",
     "sf_namespaces": ["bombora"], "hs_prefixes": ["bombora_"],
     "app_patterns": ["bombora"], "user_patterns": ["bombora"], "confidence": "low"},
    {"tool": "RollWorks", "category": "abm_intent",
     "sf_namespaces": ["rollworks"], "hs_prefixes": ["rollworks_"],
     "app_patterns": ["rollworks", "adroll"], "user_patterns": ["rollworks"],
     "confidence": "low"},
    {"tool": "Qualified", "category": "abm_intent",
     "sf_namespaces": ["qualified"], "hs_prefixes": [],
     "app_patterns": ["qualified.com", "qualified"], "user_patterns": ["qualified"],
     "confidence": "medium"},
    {"tool": "Drift", "category": "abm_intent",
     "sf_namespaces": ["drift"], "hs_prefixes": ["drift_"],
     "app_patterns": ["drift"], "user_patterns": ["drift"],
     "exact_app": True, "exact_user": True, "confidence": "medium",
     "note": "Now Salesloft Drift."},
    {"tool": "Warmly", "category": "abm_intent",
     "sf_namespaces": [], "hs_prefixes": ["warmly_"],
     "app_patterns": ["warmly"], "user_patterns": ["warmly"], "confidence": "low"},

    # -------------------------------------------------- CPQ, billing, payments
    {"tool": "Salesforce CPQ", "category": "cpq_billing",
     "sf_namespaces": ["sbqq"], "hs_prefixes": [],
     "app_patterns": ["salesforce cpq", "steelbrick"], "user_patterns": [],
     "confidence": "high", "note": "`SBQQ` is unambiguous."},
    {"tool": "Salesforce Billing", "category": "cpq_billing",
     "sf_namespaces": ["blng"], "hs_prefixes": [],
     "app_patterns": ["salesforce billing"], "user_patterns": [], "confidence": "high"},
    {"tool": "Salesforce Revenue Cloud", "category": "cpq_billing",
     "sf_namespaces": ["revenuecloud"], "hs_prefixes": [],
     "app_patterns": ["revenue cloud", "revenue lifecycle"], "user_patterns": [],
     "confidence": "low", "note": "RLM is largely core objects, so package signals are weak."},
    {"tool": "Zuora", "category": "cpq_billing",
     "sf_namespaces": ["zqu", "zuora"], "hs_prefixes": ["zuora_"],
     "app_patterns": ["zuora"], "user_patterns": ["zuora"], "confidence": "high",
     "note": "`zqu` is Zuora CPQ (Zuora Quotes)."},
    {"tool": "Conga", "category": "cpq_billing",
     "sf_namespaces": ["apxt_conga", "apxtconga4", "apttus"], "hs_prefixes": [],
     "app_patterns": ["conga", "apttus"], "user_patterns": ["conga"],
     "exact_user": True, "confidence": "medium"},
    {"tool": "DealHub", "category": "cpq_billing",
     "sf_namespaces": ["dealhub", "valooto"], "hs_prefixes": ["dealhub_"],
     "app_patterns": ["dealhub"], "user_patterns": ["dealhub"], "confidence": "low"},
    {"tool": "Subskribe", "category": "cpq_billing",
     "sf_namespaces": ["subskribe"], "hs_prefixes": [],
     "app_patterns": ["subskribe"], "user_patterns": ["subskribe"], "confidence": "low"},
    {"tool": "Chargebee", "category": "cpq_billing",
     "sf_namespaces": ["chargebee"], "hs_prefixes": ["chargebee_"],
     "app_patterns": ["chargebee"], "user_patterns": ["chargebee"], "confidence": "medium"},
    {"tool": "Stripe", "category": "cpq_billing",
     "sf_namespaces": ["stripe"], "hs_prefixes": ["stripe_"],
     "app_patterns": ["stripe"], "user_patterns": ["stripe"],
     "exact_app": True, "exact_user": True, "confidence": "medium"},
    {"tool": "Maxio", "category": "cpq_billing",
     "sf_namespaces": ["saasoptics", "maxio"], "hs_prefixes": [],
     "app_patterns": ["maxio", "saasoptics", "chargify"], "user_patterns": ["maxio"],
     "confidence": "low"},

    # ------------------------------------------------- contracts & e-signature
    {"tool": "DocuSign", "category": "contracts_esign",
     "sf_namespaces": ["dsfs", "dfsle", "docusign"], "hs_prefixes": ["docusign_"],
     "app_patterns": ["docusign"], "user_patterns": ["docusign"], "confidence": "high",
     "note": "`dsfs` classic, `dfsle` eSignature for Salesforce."},
    {"tool": "Adobe Acrobat Sign", "category": "contracts_esign",
     "sf_namespaces": ["echosign_dev1", "echosign"], "hs_prefixes": [],
     "app_patterns": ["adobe sign", "echosign", "acrobat sign"], "user_patterns": ["adobesign"],
     "confidence": "high"},
    {"tool": "PandaDoc", "category": "contracts_esign",
     "sf_namespaces": ["pandadoc"], "hs_prefixes": ["pandadoc_"],
     "app_patterns": ["pandadoc"], "user_patterns": ["pandadoc"], "confidence": "medium"},
    {"tool": "Ironclad", "category": "contracts_esign",
     "sf_namespaces": ["ironclad"], "hs_prefixes": [],
     "app_patterns": ["ironclad"], "user_patterns": ["ironclad"], "confidence": "low"},
    {"tool": "Dropbox Sign", "category": "contracts_esign",
     "sf_namespaces": ["hellosign"], "hs_prefixes": [],
     "app_patterns": ["dropbox sign", "hellosign"], "user_patterns": ["hellosign"],
     "confidence": "low"},

    # -------------------------------------------------------- customer success
    {"tool": "Gainsight", "category": "customer_success",
     "sf_namespaces": ["jbcxm", "gainsight"], "hs_prefixes": ["gainsight_"],
     "app_patterns": ["gainsight"], "user_patterns": ["gainsight"], "confidence": "high",
     "note": "`JBCXM` is the long-standing Gainsight namespace."},
    {"tool": "ChurnZero", "category": "customer_success",
     "sf_namespaces": ["churnzero"], "hs_prefixes": ["churnzero_"],
     "app_patterns": ["churnzero"], "user_patterns": ["churnzero"], "confidence": "medium"},
    {"tool": "Totango", "category": "customer_success",
     "sf_namespaces": ["totango"], "hs_prefixes": ["totango_"],
     "app_patterns": ["totango"], "user_patterns": ["totango"], "confidence": "low"},
    {"tool": "Vitally", "category": "customer_success",
     "sf_namespaces": ["vitally"], "hs_prefixes": ["vitally_"],
     "app_patterns": ["vitally"], "user_patterns": ["vitally"], "confidence": "low"},
    {"tool": "Catalyst", "category": "customer_success",
     "sf_namespaces": ["catalyst"], "hs_prefixes": [],
     "app_patterns": ["catalyst software", "catalyst.io"], "user_patterns": ["catalyst"],
     "exact_user": True, "confidence": "low"},
    {"tool": "Planhat", "category": "customer_success",
     "sf_namespaces": ["planhat"], "hs_prefixes": ["planhat_"],
     "app_patterns": ["planhat"], "user_patterns": ["planhat"], "confidence": "low"},

    # -------------------------------------------------------------- support
    {"tool": "Zendesk", "category": "support",
     "sf_namespaces": ["zendesk"], "hs_prefixes": ["zendesk_"],
     "app_patterns": ["zendesk"], "user_patterns": ["zendesk"], "confidence": "medium"},
    {"tool": "Intercom", "category": "support",
     "sf_namespaces": ["intercom"], "hs_prefixes": ["intercom_"],
     "app_patterns": ["intercom"], "user_patterns": ["intercom"], "confidence": "medium"},
    {"tool": "Freshdesk", "category": "support",
     "sf_namespaces": ["freshdesk", "fd"], "hs_prefixes": ["freshdesk_"],
     "app_patterns": ["freshdesk", "freshworks"], "user_patterns": ["freshdesk"],
     "confidence": "low"},
    {"tool": "Front", "category": "support",
     "sf_namespaces": [], "hs_prefixes": ["front_"],
     "app_patterns": ["frontapp", "front app"], "user_patterns": ["frontapp"],
     "confidence": "low", "note": "Exact patterns only — 'front' collides with everything."},

    # ---------------------------------------------------- product analytics
    {"tool": "Amplitude", "category": "product_analytics",
     "sf_namespaces": ["amplitude"], "hs_prefixes": ["amplitude_"],
     "app_patterns": ["amplitude"], "user_patterns": ["amplitude"], "confidence": "medium"},
    {"tool": "Mixpanel", "category": "product_analytics",
     "sf_namespaces": ["mixpanel"], "hs_prefixes": ["mixpanel_"],
     "app_patterns": ["mixpanel"], "user_patterns": ["mixpanel"], "confidence": "medium"},
    {"tool": "Pendo", "category": "product_analytics",
     "sf_namespaces": ["pendo"], "hs_prefixes": ["pendo_"],
     "app_patterns": ["pendo"], "user_patterns": ["pendo"],
     "exact_app": True, "exact_user": True, "confidence": "medium"},
    {"tool": "Heap", "category": "product_analytics",
     "sf_namespaces": ["heap"], "hs_prefixes": ["heap_"],
     "app_patterns": ["heap analytics"], "user_patterns": ["heap"],
     "exact_user": True, "confidence": "low"},

    # ------------------------------------------------------- iPaaS / ETL
    {"tool": "Workato", "category": "ipaas_etl",
     "sf_namespaces": ["workato"], "hs_prefixes": [],
     "app_patterns": ["workato"], "user_patterns": ["workato"], "confidence": "high"},
    {"tool": "Zapier", "category": "ipaas_etl",
     "sf_namespaces": [], "hs_prefixes": ["zapier_"],
     "app_patterns": ["zapier"], "user_patterns": ["zapier"], "confidence": "high"},
    {"tool": "Tray.ai", "category": "ipaas_etl",
     "sf_namespaces": [], "hs_prefixes": [],
     "app_patterns": ["tray.io", "tray.ai", "trayio"], "user_patterns": ["trayio", "tray-"],
     "confidence": "medium"},
    {"tool": "MuleSoft", "category": "ipaas_etl",
     "sf_namespaces": ["mulesoft"], "hs_prefixes": [],
     "app_patterns": ["mulesoft", "anypoint"], "user_patterns": ["mulesoft", "mule"],
     "exact_user": True, "confidence": "medium"},
    {"tool": "Boomi", "category": "ipaas_etl",
     "sf_namespaces": ["boomi"], "hs_prefixes": [],
     "app_patterns": ["boomi", "dell boomi"], "user_patterns": ["boomi"], "confidence": "medium"},
    {"tool": "Celigo", "category": "ipaas_etl",
     "sf_namespaces": ["celigo"], "hs_prefixes": [],
     "app_patterns": ["celigo"], "user_patterns": ["celigo"], "confidence": "medium"},
    {"tool": "Fivetran", "category": "ipaas_etl",
     "sf_namespaces": [], "hs_prefixes": [],
     "app_patterns": ["fivetran"], "user_patterns": ["fivetran"], "confidence": "high",
     "note": "Read-only replication — expect API volume with no writes."},
    {"tool": "Hightouch", "category": "ipaas_etl",
     "sf_namespaces": [], "hs_prefixes": ["hightouch_"],
     "app_patterns": ["hightouch"], "user_patterns": ["hightouch"], "confidence": "high",
     "note": "Reverse ETL — a very common source of silent field overwrites."},
    {"tool": "Census", "category": "ipaas_etl",
     "sf_namespaces": [], "hs_prefixes": ["census_"],
     "app_patterns": ["getcensus", "census"], "user_patterns": ["census"],
     "exact_user": True, "confidence": "medium",
     "note": "Reverse ETL — same overwrite risk as Hightouch."},
    {"tool": "Segment", "category": "ipaas_etl",
     "sf_namespaces": ["segment"], "hs_prefixes": ["segment_"],
     "app_patterns": ["segment", "twilio segment"], "user_patterns": ["segment"],
     "exact_user": True, "confidence": "medium"},
    {"tool": "Airbyte", "category": "ipaas_etl",
     "sf_namespaces": [], "hs_prefixes": [],
     "app_patterns": ["airbyte"], "user_patterns": ["airbyte"], "confidence": "low"},

    # ---------------------------------------------------- warehouse and BI
    {"tool": "Snowflake", "category": "warehouse_bi",
     "sf_namespaces": ["snowflake"], "hs_prefixes": [],
     "app_patterns": ["snowflake"], "user_patterns": ["snowflake"], "confidence": "medium"},
    {"tool": "Looker", "category": "warehouse_bi",
     "sf_namespaces": ["looker"], "hs_prefixes": [],
     "app_patterns": ["looker"], "user_patterns": ["looker"], "confidence": "medium"},
    {"tool": "Tableau", "category": "warehouse_bi",
     "sf_namespaces": ["tableau"], "hs_prefixes": [],
     "app_patterns": ["tableau"], "user_patterns": ["tableau"], "confidence": "medium"},
    {"tool": "Sigma", "category": "warehouse_bi",
     "sf_namespaces": [], "hs_prefixes": [],
     "app_patterns": ["sigma computing"], "user_patterns": ["sigma"],
     "exact_user": True, "confidence": "low"},
    {"tool": "Power BI", "category": "warehouse_bi",
     "sf_namespaces": [], "hs_prefixes": [],
     "app_patterns": ["power bi", "powerbi"], "user_patterns": ["powerbi"],
     "confidence": "low"},

    # --------------------------------------------------- comp and commissions
    {"tool": "Xactly", "category": "comp_planning",
     "sf_namespaces": ["xactly"], "hs_prefixes": [],
     "app_patterns": ["xactly"], "user_patterns": ["xactly"], "confidence": "medium"},
    {"tool": "CaptivateIQ", "category": "comp_planning",
     "sf_namespaces": ["captivateiq"], "hs_prefixes": [],
     "app_patterns": ["captivateiq"], "user_patterns": ["captivateiq"], "confidence": "medium"},
    {"tool": "Spiff", "category": "comp_planning",
     "sf_namespaces": ["spiff"], "hs_prefixes": [],
     "app_patterns": ["spiff"], "user_patterns": ["spiff"],
     "exact_app": True, "exact_user": True, "confidence": "low",
     "note": "Now Salesforce Spiff. Exact-match only — 'spiff' is common in field names."},
    {"tool": "QuotaPath", "category": "comp_planning",
     "sf_namespaces": ["quotapath"], "hs_prefixes": ["quotapath_"],
     "app_patterns": ["quotapath"], "user_patterns": ["quotapath"], "confidence": "low"},
    {"tool": "Everstage", "category": "comp_planning",
     "sf_namespaces": ["everstage"], "hs_prefixes": [],
     "app_patterns": ["everstage"], "user_patterns": ["everstage"], "confidence": "low"},

    # --------------------------------------------------------- enablement
    {"tool": "Highspot", "category": "enablement",
     "sf_namespaces": ["highspot"], "hs_prefixes": [],
     "app_patterns": ["highspot"], "user_patterns": ["highspot"], "confidence": "medium"},
    {"tool": "Seismic", "category": "enablement",
     "sf_namespaces": ["seismic"], "hs_prefixes": [],
     "app_patterns": ["seismic"], "user_patterns": ["seismic"], "confidence": "medium"},
    {"tool": "Showpad", "category": "enablement",
     "sf_namespaces": ["showpad"], "hs_prefixes": [],
     "app_patterns": ["showpad"], "user_patterns": ["showpad"], "confidence": "low"},
    {"tool": "Mindtickle", "category": "enablement",
     "sf_namespaces": ["mindtickle"], "hs_prefixes": [],
     "app_patterns": ["mindtickle"], "user_patterns": ["mindtickle"], "confidence": "low"},

    # ------------------------------------------------------------- comms
    {"tool": "Slack", "category": "comms",
     "sf_namespaces": ["slack"], "hs_prefixes": [],
     "app_patterns": ["slack"], "user_patterns": ["slack"],
     "exact_app": True, "exact_user": True, "confidence": "high"},
    {"tool": "Microsoft Teams", "category": "comms",
     "sf_namespaces": [], "hs_prefixes": [],
     "app_patterns": ["microsoft teams", "msteams"], "user_patterns": ["msteams"],
     "confidence": "low"},
    {"tool": "Zoom", "category": "comms",
     "sf_namespaces": ["zoom_app", "zoomapp"], "hs_prefixes": ["zoom_"],
     "app_patterns": ["zoom video", "zoom.us"], "user_patterns": ["zoom-"],
     "confidence": "low", "note": "Deliberately narrow — 'zoom' collides with ZoomInfo."},
    {"tool": "Twilio", "category": "comms",
     "sf_namespaces": ["twilio"], "hs_prefixes": ["twilio_"],
     "app_patterns": ["twilio"], "user_patterns": ["twilio"], "confidence": "medium"},

    # -------------------------------------------------------- finance / ERP
    {"tool": "NetSuite", "category": "finance_erp",
     "sf_namespaces": ["netsuite", "nscorp"], "hs_prefixes": ["netsuite_"],
     "app_patterns": ["netsuite", "oracle netsuite"], "user_patterns": ["netsuite"],
     "confidence": "medium"},
    {"tool": "Sage Intacct", "category": "finance_erp",
     "sf_namespaces": ["intacct"], "hs_prefixes": [],
     "app_patterns": ["intacct", "sage intacct"], "user_patterns": ["intacct"],
     "confidence": "medium"},
    {"tool": "Avalara", "category": "finance_erp",
     "sf_namespaces": ["ava_sfcore", "avasfcore"], "hs_prefixes": [],
     "app_patterns": ["avalara", "avatax"], "user_patterns": ["avalara"], "confidence": "high"},
    {"tool": "Anrok", "category": "finance_erp",
     "sf_namespaces": ["anrok"], "hs_prefixes": [],
     "app_patterns": ["anrok"], "user_patterns": ["anrok"], "confidence": "low"},
    {"tool": "QuickBooks", "category": "finance_erp",
     "sf_namespaces": ["quickbooks", "intuit"], "hs_prefixes": ["quickbooks_"],
     "app_patterns": ["quickbooks", "intuit"], "user_patterns": ["quickbooks"],
     "confidence": "low"},

    # ------------------------------------------------------------- ops misc
    {"tool": "Scratchpad", "category": "ops_misc",
     "sf_namespaces": ["scratchpad"], "hs_prefixes": [],
     "app_patterns": ["scratchpad"], "user_patterns": ["scratchpad"], "confidence": "medium"},
    {"tool": "Dooly", "category": "ops_misc",
     "sf_namespaces": ["dooly"], "hs_prefixes": [],
     "app_patterns": ["dooly"], "user_patterns": ["dooly"], "confidence": "low"},
    {"tool": "Sendoso", "category": "ops_misc",
     "sf_namespaces": ["sendoso"], "hs_prefixes": ["sendoso_"],
     "app_patterns": ["sendoso"], "user_patterns": ["sendoso"], "confidence": "low"},
    {"tool": "Vidyard", "category": "ops_misc",
     "sf_namespaces": ["vidyard"], "hs_prefixes": ["vidyard_"],
     "app_patterns": ["vidyard"], "user_patterns": ["vidyard"], "confidence": "low"},
    {"tool": "Jira", "category": "ops_misc",
     "sf_namespaces": ["jsfc", "atlassian"], "hs_prefixes": ["jira_"],
     "app_patterns": ["atlassian", "jira"], "user_patterns": ["jira"],
     "exact_user": True, "confidence": "low"},
    {"tool": "Asana", "category": "ops_misc",
     "sf_namespaces": ["asana"], "hs_prefixes": ["asana_"],
     "app_patterns": ["asana"], "user_patterns": ["asana"], "confidence": "low"},
    {"tool": "Salesforce Data Loader", "category": "ops_misc",
     "sf_namespaces": [], "hs_prefixes": [],
     "app_patterns": ["dataloader", "data loader", "workbench", "dataloader.io"],
     "user_patterns": ["dataloader"], "confidence": "high",
     "note": "Not a vendor integration — a human with a bulk-write tool. Worth naming anyway."},
]

# Namespace prefixes that belong to the platform itself and are never a
# third-party "tool". Filtered out before the unidentified-namespace report so
# it doesn't fill up with noise.
PLATFORM_NAMESPACES = {"", "standard", "sf", "sfdc", "salesforce", "hs", "hubspot_default"}

# Generic integration-user naming conventions. These say "this is a service
# account" without saying which vendor. Used by the integration-user section,
# not by tool detection.
SERVICE_ACCOUNT_PATTERNS = [
    "svc_", "svc-", "service.account", "serviceaccount", "service_account",
    "api_", "api-", "apiuser", "api.user", "integration", "integrations",
    "sync_", "sync-", ".sync@", "connector", "middleware", "etl_", "etl-",
    "automation@", "automated", "bot@", "noreply", "no-reply", "system@",
    "daemon", "robot", "_int@", "-int@",
]


# --------------------------------------------------------------------------- matching


def _tokens(text: str) -> List[str]:
    """Split on anything that isn't a letter or digit. 'gong-sync@acme.io' -> [gong, sync, acme, io]."""
    return [t for t in re.split(r"[^a-z0-9]+", (text or "").lower()) if t]


def _substring_hit(haystack: str, pattern: str, exact: bool) -> bool:
    """
    Substring match with the two guards that keep this table honest:
      · short patterns are ignored unless the entry opted into exact matching
      · exact matching compares whole tokens, so 'clay' hits 'clay-sync' but
        not 'barclays'
    """
    hay = (haystack or "").lower()
    pat = (pattern or "").lower()
    if not hay or not pat:
        return False
    if exact:
        toks = _tokens(hay)
        if pat in toks:
            return True
        # multi-word patterns ("chili piper") can't be a single token
        return " " in pat and pat in hay
    if len(pat) < MIN_SUBSTRING:
        return False
    return pat in hay


def _norm_ns(value: Any) -> str:
    return re.sub(r"[^a-z0-9_]", "", str(value or "").lower())


def detect(signals: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Turn raw org signals into named tool detections.

    signals = {
        "namespaces":       {"gong": "Gong for Salesforce", ...}  # ns -> package label
        "apps":             ["Gong for Salesforce", "Fivetran", ...]
        "users":            ["gong-integration@acme.com", "Zapier Service", ...]
        "field_names":      ["gong__Call__c", "SBQQ__Quote__c", ...]
        "property_names":   ["zi_company_name", "hs_lead_status", ...]   # HubSpot
    }

    Returns one dict per detected tool:
        {"tool", "category", "cluster", "confidence", "signals": [...], "signal_types": [...]}
    Sorted strongest evidence first.
    """
    namespaces = {_norm_ns(k): v for k, v in (signals.get("namespaces") or {}).items()}
    apps = [str(a) for a in (signals.get("apps") or []) if a]
    users = [str(u) for u in (signals.get("users") or []) if u]
    fields = [str(f) for f in (signals.get("field_names") or []) if f]
    props = [str(p).lower() for p in (signals.get("property_names") or []) if p]

    # Namespace prefixes actually observed on field API names (gong__Call__c -> gong).
    field_ns: Dict[str, int] = {}
    for name in fields:
        match = re.match(r"^([A-Za-z0-9_]+?)__[A-Za-z0-9_]+__c$", name) or re.match(
            r"^([A-Za-z0-9]+)__[A-Za-z0-9_]+$", name
        )
        if match:
            field_ns[_norm_ns(match.group(1))] = field_ns.get(_norm_ns(match.group(1)), 0) + 1

    out: List[Dict[str, Any]] = []
    for entry in TOOLS:
        hits: List[str] = []
        kinds: List[str] = []

        for ns in entry.get("sf_namespaces", []):
            key = _norm_ns(ns)
            if key in namespaces:
                label = namespaces[key] or key
                hits.append(f"managed package namespace `{ns}` ({label})")
                kinds.append("namespace")
            if key in field_ns:
                hits.append(f"{field_ns[key]} field(s) with the `{ns}__` prefix")
                kinds.append("namespace")

        for prefix in entry.get("hs_prefixes", []):
            matched = [p for p in props if p.startswith(prefix.lower())]
            if matched:
                hits.append(f"{len(matched)} propert(ies) prefixed `{prefix}` (e.g. {matched[0]})")
                kinds.append("property_prefix")

        for pattern in entry.get("app_patterns", []):
            for app in apps:
                if _substring_hit(app, pattern, entry.get("exact_app", False)):
                    hits.append(f"connected app / package publisher “{app}”")
                    kinds.append("app")
                    break

        for pattern in entry.get("user_patterns", []):
            for user in users:
                if _substring_hit(user, pattern, entry.get("exact_user", False)):
                    hits.append(f"integration user “{user}”")
                    kinds.append("user")
                    break

        if not hits:
            continue

        # De-duplicate while preserving order.
        seen, uniq = set(), []
        for hit in hits:
            if hit not in seen:
                seen.add(hit)
                uniq.append(hit)

        # A namespace or property-prefix hit is hard evidence; a lone name match
        # is a lead. Downgrade accordingly so the report can say which is which.
        hard = any(k in ("namespace", "property_prefix") for k in kinds)
        confidence = entry.get("confidence", "medium")
        if not hard and confidence == "high":
            confidence = "medium"
        if not hard and confidence == "medium" and len(set(kinds)) == 1:
            confidence = "low"

        out.append({
            "tool": entry["tool"],
            "category": entry["category"],
            "cluster": CLUSTERS.get(entry["category"], entry["category"]),
            "confidence": confidence,
            "signal_types": sorted(set(kinds)),
            "signals": uniq,
            "note": entry.get("note", ""),
        })

    rank = {"high": 0, "medium": 1, "low": 2}
    out.sort(key=lambda d: (rank.get(d["confidence"], 3), d["cluster"], d["tool"]))
    return out


def unidentified_namespaces(
    namespaces: Dict[str, str], detected: Sequence[Dict[str, Any]]
) -> List[Dict[str, str]]:
    """
    Namespaces present in the org that no table entry claims. These are NOT
    dropped — an unrecognised package with write access is exactly the thing
    this plugin exists to surface.
    """
    claimed = set()
    for entry in TOOLS:
        if any(e["tool"] == entry["tool"] for e in detected):
            claimed.update(_norm_ns(n) for n in entry.get("sf_namespaces", []))
    return [
        {"namespace": ns, "package": label or "(name unavailable)"}
        for ns, label in sorted(namespaces.items())
        if _norm_ns(ns) not in claimed and _norm_ns(ns) not in PLATFORM_NAMESPACES
    ]


def looks_like_service_account(*values: Any) -> bool:
    """Generic 'this is a robot, not a person' test across name / username / email."""
    blob = " ".join(str(v or "").lower() for v in values)
    return any(p in blob for p in SERVICE_ACCOUNT_PATTERNS)


def match_believed(
    believed: Iterable[str], detected: Sequence[Dict[str, Any]]
) -> Dict[str, List[str]]:
    """
    Reconcile what the customer said in setup against what the org shows.

    Returns {"confirmed": [...], "undisclosed": [...], "claimed_not_found": [...]}
    Names are compared loosely (case, punctuation and spacing folded) because
    people type "chili piper", "ChiliPiper" and "Chili Piper".
    """
    def key(value: str) -> str:
        return re.sub(r"[^a-z0-9]", "", str(value or "").lower())

    detected_keys = {key(d["tool"]): d["tool"] for d in detected}
    believed_list = [b for b in (believed or []) if str(b).strip()]

    confirmed, claimed_not_found = [], []
    matched_detected = set()
    for item in believed_list:
        k = key(item)
        hit: Optional[str] = None
        for dk, name in detected_keys.items():
            if k and (k == dk or k in dk or dk in k):
                hit = name
                break
        if hit:
            confirmed.append(hit)
            matched_detected.add(hit)
        else:
            claimed_not_found.append(str(item))

    undisclosed = [d["tool"] for d in detected if d["tool"] not in matched_detected]
    return {
        "confirmed": sorted(set(confirmed)),
        "undisclosed": undisclosed,
        "claimed_not_found": claimed_not_found,
    }


if __name__ == "__main__":  # pragma: no cover - quick manual sanity check
    import json as _json

    demo = detect({
        "namespaces": {"gong": "Gong for Salesforce", "SBQQ": "Salesforce CPQ", "abcxyz": "Acme Widget"},
        "apps": ["Fivetran", "Legacy Data Loader (Prod)"],
        "users": ["zi-sync@acme.com.integration", "Barclays Ops"],
        "field_names": ["gong__Call__c", "SBQQ__Quote__c"],
        "property_names": ["zi_company_name"],
    })
    print(_json.dumps(demo, indent=2))
    print(_json.dumps(unidentified_namespaces(
        {"gong": "Gong for Salesforce", "abcxyz": "Acme Widget"}, demo), indent=2))
