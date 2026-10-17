Start SAP systems : 

# su - orasre
 # > sqlplus / as  sysdba
 # > startup**
# lsnrctl stop LISTENER
# su - sreadm
# startsap

a. Start Data collectors : Startup sequence
SAP Host Exporter
        ↓
Oracle DB Exporter
        ↓
      Alloy
b. Steps to Start Data collectors 
# 1. Start SAP Host Exporter 
# start service 
systemctl start prometheus-sap_host_exporter@D00 
# Check status
systemctl status prometheus-sap_host_exporter@D00   

# 2. Start Oracle DB Exporter 
# start  service 
sudo systemctl start oracle-db-exporter 
# Check status
sudo systemctl status oracle-db-exporter 

  
 3. Start Alloy 
>> start  service 
# sudo systemctl start alloy --no-pager 
 Check status
# sudo systemctl status alloy --no-pager 

Stop Sequence 

Stop the components in the reverse order:  
1. Stop Alloy 
Stop service 
# sudo systemctl stop alloy --no-pager 
Check status
# sudo systemctl status alloy --no-pager 

  
2. Stop Oracle DB Exporter 
#Stop service 
# sudo systemctl stop oracle-db-exporter 
#Check status
# sudo systemctl status oracle-db-exporter 

  
 3. Stop SAP Host Exporter 
 Stop service 
# systemctl stop prometheus-sap_host_exporter@D00 
Check status
# systemctl status prometheus-sap_host_exporter@D00 

Summary 

