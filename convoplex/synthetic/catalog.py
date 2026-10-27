"""Fictional shops and an illustrative catalogue. Prices are whole naira, not live market quotes."""

from __future__ import annotations

RETAILERS = (
    {
        "name": "Mama Nkechi Provisions",
        "city": "Lagos",
        "area": "Ikeja",
        "category_focus": "provisions",
        "phone": "+2348000001001",
    },
    {
        "name": "Alhaja Bisi Textiles",
        "city": "Lagos",
        "area": "Lagos Island",
        "category_focus": "ankara",
        "phone": "+2348000001002",
    },
    {
        "name": "Computer Village Mobile",
        "city": "Lagos",
        "area": "Ikeja",
        "category_focus": "phones",
        "phone": "+2348000001003",
    },
    {
        "name": "Wuse Fresh Basket",
        "city": "Abuja",
        "area": "Wuse 2",
        "category_focus": "provisions",
        "phone": "+2348000001004",
    },
    {
        "name": "GRA Lace & Ankara",
        "city": "Port Harcourt",
        "area": "GRA Phase 2",
        "category_focus": "ankara",
        "phone": "+2348000001005",
    },
    {
        "name": "Garki Gadget Hub",
        "city": "Abuja",
        "area": "Garki",
        "category_focus": "phones",
        "phone": "+2348000001006",
    },
)

AREAS = {
    "Lagos": ("Ikeja", "Yaba", "Surulere", "Lagos Island", "Festac", "Ajah", "Maryland", "Gbagada"),
    "Abuja": ("Wuse 2", "Garki", "Gwarinpa", "Wuse", "Maitama", "Kubwa", "Lugbe"),
    "Port Harcourt": ("GRA Phase 2", "Rumuola", "Trans Amadi", "D-Line", "Rumuokwuta", "Eliozu"),
}

# (sku, name, category, unit, base_price_ngn, core)
PROVISIONS = (
    ("RICE-50", "Mama Gold Rice 50kg", "provisions", "bag", 82000, True),
    ("GARRI-PAINT", "Garri Ijebu paint bucket", "provisions", "bucket", 4500, True),
    ("PALM-5L", "Palm oil 5 litres", "provisions", "keg", 12500, True),
    ("INDOMIE-40", "Indomie Chicken carton", "provisions", "carton", 9800, True),
    ("RICE-25", "Cap Rice 25kg", "provisions", "bag", 44000, False),
    ("SEMO-10", "Golden Penny Semovita 10kg", "provisions", "bag", 18500, False),
    ("GNO-5L", "King's groundnut oil 5 litres", "provisions", "keg", 14800, False),
    ("PEAK-TIN", "Peak Milk tin 400g", "provisions", "tin", 1450, False),
    ("PEAK-CTN", "Peak Milk carton", "provisions", "carton", 16800, False),
    ("SUGAR-1", "Dangote Sugar 1kg", "provisions", "pack", 1750, False),
    ("MAGGI-CTN", "Maggi Star carton", "provisions", "carton", 8600, False),
    ("TITUS", "Titus sardine", "provisions", "tin", 900, False),
    ("MILO-400", "Milo refill 400g", "provisions", "pack", 3200, False),
    ("BEANS-PAINT", "Beans oloyin paint bucket", "provisions", "bucket", 12500, False),
    ("EGUSI-1", "Egusi melon 1kg", "provisions", "pack", 4800, False),
    ("CRAY-500", "Ground crayfish 500g", "provisions", "pack", 6500, False),
    ("POUNDO", "Poundo Yam 900g", "provisions", "pack", 3600, False),
    ("HYPO-1", "Hypo bleach 1 litre", "provisions", "bottle", 1200, False),
    ("ARIEL-900", "Ariel detergent 900g", "provisions", "pack", 2800, False),
)

ANKARA = (
    ("ANK-HIT", "Hitarget Ankara 6 yards", "ankara", "piece", 8500, True),
    ("ANK-ABC", "ABC Wax 6 yards", "ankara", "piece", 13500, True),
    ("ANK-GUI", "Guinea brocade 5 yards", "ankara", "piece", 24000, True),
    ("ANK-ADI", "Adire kampala 6 yards", "ankara", "piece", 16000, True),
    ("ANK-VLI", "Vlisco Hollandais 6 yards", "ankara", "piece", 52000, False),
    ("ANK-DAV", "Daviva Wax 6 yards", "ankara", "piece", 15000, False),
    ("ANK-LACE", "Swiss lace 5 yards", "ankara", "piece", 68000, False),
    ("ANK-SEN", "Senator fabric 4 yards", "ankara", "piece", 18000, False),
    ("ANK-OKE", "Aso-oke set", "ankara", "set", 35000, False),
    ("ANK-VEL", "Velvet lace 5 yards", "ankara", "piece", 29500, False),
)

PHONES = (
    ("ACC-BANK", "Oraimo 20000mAh power bank", "accessories", "piece", 18500, True),
    ("ACC-BUDS", "Oraimo FreePods", "accessories", "piece", 8500, True),
    ("ACC-CHRG", "33W fast charger", "accessories", "piece", 6500, True),
    ("ACC-GLASS", "Tempered glass screen guard", "accessories", "piece", 2000, True),
    ("PHN-CAMON", "Tecno Camon 30", "phones", "piece", 285000, False),
    ("PHN-HOT", "Infinix Hot 40", "phones", "piece", 165000, False),
    ("PHN-ITEL", "Itel A70", "phones", "piece", 98000, False),
    ("PHN-A15", "Samsung Galaxy A15", "phones", "piece", 215000, False),
    ("ACC-CASE", "Phone pouch", "accessories", "piece", 3500, False),
    ("ACC-SPKR", "Oraimo portable speaker", "accessories", "piece", 15500, False),
    ("ACC-SD", "128GB memory card", "accessories", "piece", 12000, False),
    ("ACC-CABLE", "USB-C cable", "accessories", "piece", 3000, False),
)

CATALOGUES = {
    "provisions": PROVISIONS,
    "ankara": ANKARA,
    "phones": PHONES,
}

PRICE_BUMPS = (0, 2, -2, 3, -3, 1)

FIRST_NAMES = (
    "Chinedu", "Emeka", "Tunde", "Adewale", "Ibrahim", "Musa", "Chukwuemeka", "Olumide",
    "Segun", "Kunle", "Abdullahi", "Yusuf", "Ifeanyi", "Obinna", "Femi", "Babatunde",
    "Chidi", "Nnamdi", "Seyi", "Kayode", "Uche", "Nonso", "Tochukwu", "Adebayo",
    "Oluwaseun", "Hassan", "Aliyu", "Garba", "Kelechi", "Ngozi", "Chioma", "Funmilayo",
    "Aisha", "Fatima", "Adaeze", "Yetunde", "Blessing", "Amaka", "Zainab", "Halima",
    "Bukola", "Titilayo", "Chiamaka", "Ifeoma", "Nneka", "Amina", "Maryam", "Oluwakemi",
    "Folake", "Chinyere", "Hadiza", "Bisola", "Temitope", "Adenike", "Grace", "Patience",
)

SURNAMES = (
    "Okonkwo", "Adeyemi", "Mohammed", "Okafor", "Balogun", "Ibrahim", "Nwosu", "Ogunleye",
    "Bello", "Eze", "Adebayo", "Musa", "Okeke", "Lawal", "Chukwu", "Abdullahi",
    "Ojo", "Nwachukwu", "Yusuf", "Akinwale", "Obi", "Suleiman", "Igwe", "Adeleke",
    "Okoro", "Danjuma", "Umeh", "Bakare", "Nwankwo", "Aliyu", "Oladipo", "Ekwueme",
)

PHONE_PREFIXES = (
    "803", "806", "813", "810", "703", "706", "802", "809", "705", "807",
    "814", "816", "903", "905", "907", "908", "909", "812", "815", "818",
    "701", "708", "902", "904", "913",
)


def adjusted_price(base: int, retailer_slot: int) -> int:
    bump = PRICE_BUMPS[retailer_slot % len(PRICE_BUMPS)]
    return max(1, base * (100 + bump) // 100)
