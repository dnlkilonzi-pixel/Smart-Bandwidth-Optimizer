from setuptools import setup, find_packages

setup(
    name="smart-bandwidth-optimizer",
    version="2.0.0",
    description=(
        "A production-grade system that prioritizes important traffic, "
        "compresses data, drops unnecessary packets, tracks flows, "
        "supports YAML policy rules, and exposes a real-time telemetry API."
    ),
    packages=find_packages(exclude=["tests*"]),
    python_requires=">=3.9",
    install_requires=[
        "fastapi>=0.100.0",
        "uvicorn[standard]>=0.23.0",
        "pyyaml>=6.0",
    ],
    extras_require={
        "dev": ["pytest>=7.0"],
        "pcap": ["scapy"],
        "nfqueue": ["netfilterqueue"],
    },
    entry_points={
        "console_scripts": [
            "bandwidth-optimizer=main:main",
        ],
    },
    classifiers=[
        "Programming Language :: Python :: 3",
        "Operating System :: OS Independent",
    ],
)
