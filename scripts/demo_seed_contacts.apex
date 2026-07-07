// Clean any prior demo seed, then insert duplicates bypassing the org's duplicate rules.
delete [SELECT Id FROM Contact WHERE Department = 'DEMOSEED0707'];

Database.DMLOptions dml = new Database.DMLOptions();
dml.DuplicateRuleHeader.allowSave = true;
dml.DuplicateRuleHeader.runAsCurrentUser = true;

List<Contact> cs = new List<Contact>{
  // Cluster 1: Robert/Bob Smith — identical email, gap-fill phone+title
  new Contact(FirstName='Robert', LastName='Smith', Email='robert.smith@acme.com', Phone='4155550101', Title='VP Sales', Department='DEMOSEED0707'),
  new Contact(FirstName='Bob',    LastName='Smith', Email='robert.smith@acme.com', Department='DEMOSEED0707'),
  // Cluster 2: Jennifer/Jenn Lee — same domain, near-identical
  new Contact(FirstName='Jennifer', LastName='Lee', Email='jennifer.lee@globex.com', Phone='2125550133', Title='Marketing Director', Department='DEMOSEED0707'),
  new Contact(FirstName='Jenn',     LastName='Lee', Email='jenn.lee@globex.com', Phone='2125550142', Department='DEMOSEED0707'),
  // Cluster 3: Michael/Mike Chen — identical email, nickname
  new Contact(FirstName='Michael', LastName='Chen', Email='mchen@initech.com', Title='Engineering Lead', Department='DEMOSEED0707'),
  new Contact(FirstName='Mike',    LastName='Chen', Email='mchen@initech.com', Phone='4085550199', Department='DEMOSEED0707'),
  // Cluster 4: Sarah Johnson x2 — same name, diff email same domain
  new Contact(FirstName='Sarah', LastName='Johnson', Email='sjohnson@umbrella.com', Phone='3125550170', Title='CFO', Department='DEMOSEED0707'),
  new Contact(FirstName='Sarah', LastName='Johnson', Email='sarah.johnson@umbrella.com', Title='Chief Financial Officer', Department='DEMOSEED0707'),
  // Cluster 5: David/Dave/D Martinez — TRIPLE
  new Contact(FirstName='David', LastName='Martinez', Email='dmartinez@hooli.com', Title='Product Manager', Department='DEMOSEED0707'),
  new Contact(FirstName='Dave',  LastName='Martinez', Email='dmartinez@hooli.com', Phone='6505550188', Department='DEMOSEED0707'),
  new Contact(FirstName='D',     LastName='Martinez', Email='david.martinez@hooli.com', Phone='6505550188', Title='Sr Product Manager', Department='DEMOSEED0707'),
  // Non-duplicates (realism)
  new Contact(FirstName='Emily', LastName='Nguyen',  Email='enguyen@stark.com', Title='Data Scientist', Department='DEMOSEED0707'),
  new Contact(FirstName='James', LastName='Brennan', Email='jbrennan@wayne.com', Phone='7185550164', Department='DEMOSEED0707'),
  new Contact(FirstName='Priya', LastName='Patel',   Email='ppatel@cyberdyne.com', Title='Operations Lead', Department='DEMOSEED0707')
};
List<Database.SaveResult> rs = Database.insert(cs, dml);
Integer ok = 0; for (Database.SaveResult r : rs) { if (r.isSuccess()) ok++; }
System.debug('SEED_INSERTED=' + ok + '/' + cs.size());
