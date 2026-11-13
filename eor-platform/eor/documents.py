"""Contract generation from country rules. Output is plain text sent to the e-sign adapter."""


def contract(worker, client, rules):
    eor = worker["engagement_type"] == "EOR"
    title = "EMPLOYMENT AGREEMENT" if eor else "INDEPENDENT CONTRACTOR AGREEMENT"
    employer = "LanceSoft EOR (Employer of Record)" if eor else "LanceSoft EOR (Agent of Record)"
    if worker.get("rate_hourly"):
        pay = f"{worker['currency']} {worker['rate_hourly']} per hour, invoiced monthly"
    else:
        pay = f"{worker['currency']} {worker['salary_annual']} per year, paid monthly"
    lines = [
        title, "",
        f"Between: {employer}",
        f"And: {worker['full_name']} <{worker['email']}>",
        f"Assigned to client: {client['name']}",
        f"Jurisdiction: {rules['name']}", "",
        f"1. Role: {worker.get('job_title') or 'As agreed with client'}",
        f"2. Start date: {worker['start_date']}" + (f"; end date: {worker['end_date']}" if worker.get("end_date") else ""),
        f"3. Compensation: {pay}",
    ]
    if eor:
        lines += [
            f"4. Probation period: {rules['probation_days']} days",
            f"5. Notice period: {rules['notice_days']} days",
            f"6. Paid annual leave: {rules['annual_leave_days']} days",
            f"7. Benefits: {', '.join(rules['benefits'])}",
            f"8. Right to work: {rules['right_to_work']} required before start",
        ]
    else:
        lines += [
            "4. Contractor controls the manner and means of performing the services.",
            "5. Contractor is responsible for own taxes, insurance and equipment.",
            "6. Contractor may provide services to other clients and may subcontract with notice.",
        ]
    lines += ["", "Country-specific terms:"] + [f"- {c}" for c in rules["contract_clauses"]]
    return "\n".join(lines)
