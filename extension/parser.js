(function initializeParser(root) {
  "use strict";

  const TITLE_SELECTORS = [
    "h1[data-test-id='job-title']",
    "[data-automation-id='jobPostingHeader']",
    ".posting-headline h2",
    "#content h1",
    "h1.top-card-layout__title",
    "h1.jobsearch-JobInfoHeader-title",
    "h1",
  ];
  const COMPANY_SELECTORS = [
    "[data-company-name]",
    "[data-automation-id='company']",
    ".posting-headline [class*='company']",
    "#content [class*='company']",
    ".topcard__org-name-link",
    ".jobsearch-InlineCompanyRating-companyHeader",
    "[class*='company-name']",
    "[class*='companyName']",
  ];
  const DESCRIPTION_SELECTORS = [
    "[data-test-id='job-description']",
    "[data-automation-id='jobPostingDescription']",
    "#content .job-post",
    ".section-wrapper",
    "#job-details",
    "#jobDescriptionText",
    ".show-more-less-html__markup",
    "[class*='job-description']",
    "[class*='jobDescription']",
    "main",
  ];
  const LOCATION_SELECTORS = [
    "[data-automation-id='locations']",
    "[data-test-id='job-location']",
    ".topcard__flavor--bullet",
    "[class*='job-location']",
    "[class*='jobLocation']",
    ".loc",
  ];

  function cleanText(value) {
    return typeof value === "string" ? value.replace(/\s+/g, " ").trim() : "";
  }

  function firstText(document, selectors) {
    for (const selector of selectors) {
      const value = cleanText(document.querySelector(selector)?.textContent);
      if (value) return value;
    }
    return "";
  }

  function jsonLdObjects(document) {
    const objects = [];
    for (const script of document.querySelectorAll("script[type='application/ld+json']")) {
      try {
        const parsed = JSON.parse(script.textContent || "null");
        const queue = Array.isArray(parsed) ? [...parsed] : [parsed];
        while (queue.length) {
          const item = queue.shift();
          if (!item || typeof item !== "object") continue;
          objects.push(item);
          if (Array.isArray(item["@graph"])) queue.push(...item["@graph"]);
        }
      } catch (_error) {
        // Broken third-party JSON-LD must not prevent DOM extraction.
      }
    }
    return objects;
  }

  function jobPosting(document) {
    return jsonLdObjects(document).find((item) => {
      const types = Array.isArray(item["@type"]) ? item["@type"] : [item["@type"]];
      return types.includes("JobPosting");
    });
  }

  function organizationName(value) {
    if (typeof value === "string") return cleanText(value);
    return cleanText(value?.name);
  }

  function plainDescription(value, document) {
    if (!value) return "";
    const container = document.createElement("div");
    container.innerHTML = String(value);
    return cleanText(container.textContent);
  }

  function salaryFacts(baseSalary) {
    const value = baseSalary?.value || baseSalary;
    if (!value || typeof value !== "object") return {};
    const unit = cleanText(value.unitText || baseSalary?.unitText).toUpperCase();
    if (unit && !["YEAR", "YEARLY", "ANNUAL"].includes(unit)) return {};
    const minimum = Number(value.minValue ?? value.value);
    const maximum = Number(value.maxValue ?? value.value);
    const result = {};
    if (Number.isFinite(minimum) && minimum >= 0) {
      result.base_salary_min_usd = Math.round(minimum);
    }
    if (Number.isFinite(maximum) && maximum >= 0) {
      result.base_salary_max_usd = Math.round(maximum);
    }
    return result;
  }

  function locationFacts(structured) {
    const remote = cleanText(structured?.jobLocationType).toUpperCase().includes("TELECOMMUTE");
    const rawLocations = Array.isArray(structured?.jobLocation)
      ? structured.jobLocation
      : [structured?.jobLocation];
    const locations = [];
    for (const item of rawLocations) {
      const address = item?.address || item;
      if (!address || typeof address !== "object") continue;
      const rendered = [
        address.addressLocality,
        address.addressRegion,
        address.addressCountry,
      ].map(cleanText).filter(Boolean).join(", ");
      if (rendered && !locations.includes(rendered)) locations.push(rendered);
    }
    if (remote && !locations.length) {
      const requirements = Array.isArray(structured?.applicantLocationRequirements)
        ? structured.applicantLocationRequirements
        : [structured?.applicantLocationRequirements];
      for (const requirement of requirements) {
        const name = cleanText(requirement?.name || requirement);
        if (name && !locations.includes(name)) locations.push(name);
      }
    }
    const output = {};
    if (locations.length || remote) output.location = locations.join("; ") || "Remote";
    if (remote) output.workplace_type = "Remote";
    return output;
  }

  function sponsorshipStatus(text) {
    const normalized = cleanText(text).toLowerCase();
    const noPatterns = [
      /\bno (?:visa )?sponsorship\b/,
      /\bwithout (?:visa )?sponsorship\b/,
      /\b(?:unable to|will not|cannot|can't) (?:provide|offer) (?:visa )?sponsorship\b/,
      /\bnot eligible for (?:visa |employment )?sponsorship\b/,
    ];
    if (noPatterns.some((pattern) => pattern.test(normalized))) return "NO";
    const yesPatterns = [
      /\bvisa sponsorship (?:is )?(?:available|provided|offered)\b/,
      /\bwill (?:provide|offer) (?:visa )?sponsorship\b/,
    ];
    return yesPatterns.some((pattern) => pattern.test(normalized)) ? "YES" : "UNKNOWN";
  }

  function extract(document, pageUrl) {
    const structured = jobPosting(document) || {};
    const title = cleanText(structured.title) || firstText(document, TITLE_SELECTORS);
    const company = organizationName(structured.hiringOrganization)
      || firstText(document, COMPANY_SELECTORS);
    const description = plainDescription(structured.description, document)
      || firstText(document, DESCRIPTION_SELECTORS);
    const structuredLocation = locationFacts(structured);
    const domLocation = firstText(document, LOCATION_SELECTORS);

    if (!title || !company || !description) {
      const missing = [
        !title && "title",
        !company && "company",
        !description && "description",
      ].filter(Boolean);
      throw new Error(`Could not extract required job fields: ${missing.join(", ")}.`);
    }

    return {
      page_url: pageUrl,
      posting: {
        title,
        company,
        description,
        ...(structuredLocation.location
          ? structuredLocation
          : (domLocation ? { location: domLocation } : {})),
        sponsorship_status: sponsorshipStatus(`${title}\n${company}\n${description}`),
        ...salaryFacts(structured.baseSalary),
      },
    };
  }

  const api = { cleanText, extract, locationFacts, salaryFacts, sponsorshipStatus };
  root.JobRadarParser = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
