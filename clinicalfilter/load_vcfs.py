'''
Copyright (c) 2016 Genome Research Ltd.

Permission is hereby granted, free of charge, to any person obtaining a copy of
this software and associated documentation files (the "Software"), to deal in
the Software without restriction, including without limitation the rights to
use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies
of the Software, and to permit persons to whom the Software is furnished to do
so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS
FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR
COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER
IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
'''

import logging

from clinicalfilter.variant.info import Info
from clinicalfilter.variant.variant import Variant
from clinicalfilter.variant.snv import SNV
from clinicalfilter.variant.cnv import CNV
from clinicalfilter.trio_genotypes import TrioGenotypes
from clinicalfilter.utils import open_vcf, get_vcf_header, exclude_header, \
    construct_variant
from clinicalfilter.multinucleotide_variants import get_mnv_candidates

def load_variants(family, pp_filter, pops, known_genes, last_base, sum_x_lr2,
        debug_chrom=None, debug_pos=None):
    """ loads the variants for a trio or singleton
    
    Args:
        family: Family object containing an data for an affected proband
        pp_filter: float between 0 and 1, being the threshold for the PP_DNM filter
        pops: list of populations who have minor allele frequencies in INFO
        known_genes: genes known to be involved with genetic disorders.
        last_base: set of sites in genome at conserved last base of exons,
            where we upgrade the severity of variants to loss-of-function.
        debug_chrom: chromosome string, to give more information about why
            a variant fails to pass the filters.
        debug_pos: chromosome position, to give more information about why
            a variant fails to pass the filters.
        sum_x_lr2: Sum of mean l2r on x chromosomes for all probands
    
    Returns:
        list of filtered variants for a trio, as TrioGenotypes objects
    """

    parents = family.has_parents()
    
    # define several parameters of the variant classes, before initialisation
    for Var in [SNV, CNV]:
        Var.set_known_genes(known_genes)
        Var.set_debug(debug_chrom, debug_pos)
    
    Info.set_last_base_sites(last_base)
    Info.set_populations(pops)

#get sum of mean l2r for proband
    sum_x_lr2_proband = 0
    if family.child.person_id in sum_x_lr2.keys():
        sum_x_lr2_proband = sum_x_lr2[family.child.person_id]
    
    variants = load_trio(family, sum_x_lr2_proband)
    
    return filter_de_novos(variants, pp_filter)
    
def include_variant(line, child_variants, gender, mnvs, sum_x_lr2, parents):
    """ check if we want to include the variant or not
    
    Args:
        line: list of elements from the VCF line for the variant.
        child_variants: list of variants that passed in the child, so we can
            quickly assess parental variants. This is None when screening
            the child.
        gender: the gender of the proband (used in CNV filtering).
        mnvs: dictionary of (chrom, pos), MNV_code pairs for known
            multinucleotide variant sites  within the proband.
        sum_x_lr2: SUm of mean lr2 on x chromosome for proband.
        parents: does trio have parents?
    
    Returns:
        True/False for whether to include the variant.
    """
    
    if child_variants is not None:
        key = (line[0], int(line[1]))
        return key in child_variants
    
    var = construct_variant(line, gender, mnvs, sum_x_lr2, parents)
    return var.passes_filters()
    
def open_individual(individual, child_variants=None, mnvs=None, sum_x_lr2=None, parents=None):
    """ Convert VCF to TSV format. Use for single sample VCF file.
    
    Obtains the VCF data for a single sample. This function optionally
    filters the lines of the VCF file that pass defined criteria, in order
    to reduce memory usage.
    
    Args:
        individual: Person object for individual
        child_variants: True/False for whether variants have been filtered
            for the proband (if so, we can simply check the parent's
            variants for matches in the child's variants).
        mnvs: dictionary
        sum_x_lr2: Sum of mean lr2 for proband X chromosome for filtering CNVs
        parents: does the family have both parents?
    
    Returns:
        A list of variants for the individual.
    """

#    parents = individual.has_parents()

    if individual is None:
        return []
    
    path = individual.get_path()
    logging.info("sample path: {}".format(path))
    gender = individual.get_gender()
    
    # open the vcf, and adjust the position in the file to immediately after
    # the header, so we can run through the variants
    vcf = open_vcf(path)
    exclude_header(vcf)
    
    variants = []
    for line in vcf:
        line = line.strip().split("\t")
        
        try:
            # check if we want to include the variant or not
            if include_variant(line, child_variants, gender, mnvs, sum_x_lr2, parents):
                var = construct_variant(line, gender, mnvs, sum_x_lr2, parents)
                var.add_vcf_line(line)
                variants.append(var)
        except ValueError:
            # we only get ValueError when the genotype cannot be set, which
            # occurs for x chrom male heterozygotes (an impossible genotype)
            if line[0] == SNV.debug_chrom and int(line[1]) == SNV.debug_pos:
                print("failed as heterozygous genotype in male on chrX")
            continue
    
    vcf.close()
    
    return variants

def load_trio(family, sum_x_lr2_proband):
    """ opens and parses the VCF files for members of the family trio.
    
    We need to load the VCF data for each of the members of the trio. As a
    bare minimum we need VCF data for the child in the family. Occasionally
    we lack parents for the child, so we create blank entries when that
    happens.
    We also need the sum of mean lr2 ratios on the X chromosome for the proband
    """
    
    mnvs = get_mnv_candidates(family.child.get_path())
    
    # open the childs VCF file, and get the variant keys, to check if they
    # are in the parents VCF
    parents = family.has_parents()

    child = open_individual(family.child, mnvs=mnvs, sum_x_lr2=sum_x_lr2_proband, parents=parents)
    keys = set([var.get_key() for var in child])
    
    mother = open_individual(family.mother, child_variants=keys)
    father = open_individual(family.father, child_variants=keys)
    
    return combine_trio_variants(family, child, mother, father)

def combine_trio_variants(family, child_vars, mother_vars, father_vars):
    """ for each variant, combine the trio's genotypes into TrioGenotypes
    
    Args:
        child_vars: list of Variant objects for the child
        mother_vars: list of Variant objects for the mother
        father_vars: list of Variant objects for the father
    
    Returns:
        list of TrioGenotypes objects for the family
    """
    
    variants = []
    for child in child_vars:
        
        mom, dad = None, None
        if family.has_parents():
            mom = get_parental_var(child, mother_vars, family.mother)
            dad = get_parental_var(child, father_vars, family.father)
        
        trio = TrioGenotypes(child.get_chrom(), child.get_position(),
            child, mom, dad, SNV.debug_chrom, SNV.debug_pos)
        
        variants.append(trio)
    
    return variants

def get_parental_var(var, parental_vars, parent):
    """ get the corresponding parental variant to a childs variant, or
    create a default variant with reference genotype.
    
    Args:
        var: childs var, as Variant object
        parental_vars: list of parental variants
        parent: Person object for the parent
    
    Returns:
        returns a Variant object, matched to the proband's variant
    """
    
    key = var.get_key()
    
    for parental in parental_vars:
        if not var.is_cnv() and key == parental.get_key():
            return parental
    
    # if the childs variant does not exist in the parents VCF, then we
    # create a default variant for the parent
    Var = SNV
    keys, sample = 'GT', '0/0'
    alts = ','.join(var.alt_alleles)
    
    if var.is_cnv():
        Var = CNV
        inh = var.get_cnv_inheritance()
        alts = ("<REF>", )
        if parent.is_male() and inh in ['paternal', 'biparental']:
            alts = var.alt_alleles
        elif parent.is_female() and inh in ['maternal', 'biparental']:
            alts = var.alt_alleles
        
        alts = ','.join(alts)
        # we need to set a format value, so CNV genotypes get set correctly
        keys, sample = 'INHERITANCE', 'uncertain'
    
    return Var(var.chrom, var.position, var.variant_id, var.ref_allele,
        alts, var.qual, var.filter, str(var.info), keys, sample,
        parent.get_gender())

def filter_de_novos(variants, pp_filter):
    """ filter the de novos variants in the VCF files
    
    Args:
        variants: list of TrioGenotypes objects.
        pp_filter float between 0 and 1, being the threshold for the PP_DNM filter
    
    Returns:
        a list of TrioGenotypes without the de novo variants that failed the
        de novo filter.
    """
    
    return [ x for x in variants if x.passes_de_novo_checks(pp_filter) ]
