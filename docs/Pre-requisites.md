## How the TCP/IP Model Actually Works

### Protocols & Standards

Protocols  
- A protocol is a set of rules defining how data should be communicated between devices over a network.  
- Protocols are the "languages" that computers use to communicate.  
- Proprietary protocols (e.g., IBM, Apple) made cross-vendor communication difficult or impossible.  

Standards  
- A standard is an agreed-upon specification that describes how a protocol or technology should work.  
- Standard, vendor-neutral protocols allow devices from different vendors to communicate seamlessly (e.g., Apple MacBook accessing a Linux web server).

### History of TCP/IP  

- 1960s–1970s: US Department of Defense ARPA (Advanced Research Projects Agency) funded ARPANET, which came online in 1969 connecting mainframes at universities and labs.  
- ARPANET originally used a protocol called NCP (Network Control Program).  
- 1974: Vint Cerf and Bob Kahn began developing TCP (Transmission Control Program).  
- TCP was later divided into two protocols:  
    1. TCP (Transmission Control Protocol)  
    2. IP (Internet Protocol)  
- Together these 2 protocols form the foundation of the protocol suite known as TCP/IP today. 
- January 1, 1983: ARPANET fully switched to TCP/IP.  
- TCP/IP became dominant because it was published as a set of open standards that any vendor could implement and it could run over many network types.

### Standards Organizations

Most networking standards are created by independent standards organizations, not single vendors.  

IEEE (Institute of Electrical and Electronics Engineers)  

- Defines many technologies used on LAN such as Ethernet (IEEE 802.3) and Wi-Fi (IEEE 802.11).
- Specifies physical attributes (cables, radio frequencies) and message formatting over the physical medium.

IETF (Internet Engineering Task Force)  

- Defines protocols used on the Internet: TCP, IP, UDP, HTTP, DNS etc.  
- Publishes standards in documents called RFCs (Request for Comments), which are freely available online.

IEEE and IETF create the standards and vendors like Cisco implement them so that devices from different companies can work together.