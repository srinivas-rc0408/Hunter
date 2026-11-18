"""Job sources. Each returns list[Job] and appends human-readable errors to a list.

boards  – LinkedIn, Indeed, Naukri, Glassdoor, Google Jobs (JobSpy, logged out)
ats     – company boards: Greenhouse, Lever, Ashby, Workday (hunter-scout parsers)
pages   – any careers/notice page, diffed for new links (Birla, govt sites)
govt    – government radar on top of pages + eligibility rules
"""
